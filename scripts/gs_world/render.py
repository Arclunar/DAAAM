"""Render an RGB-D sequence from a trained gs_world model, in the dataset layout DAAAM reads.

	docker compose run --rm -T daaam-ws python -m scripts.gs_world.render /data/gs_world/models/tum_fr1_room/<time>
	(just gs-render <model>; 'latest' picks the newest model under /data/gs_world/models)

Trajectories (--trajectory):
	original   every frame of the training dataset, in order, with its timestamps (default)
	test       only the frames held out from training
	<file>     16 numbers per line (row-major T_world_camera, like pose/poses.txt) or TUM 'time tx ty tz qx qy qz qw'
--offset X,Y,Z (metres) and --rotate RX,RY,RZ (degrees) move every camera in its own optical frame (x right, y down,
z forward): --offset 0,-0.2,0 flies the same path 20 cm higher, --rotate 0,15,0 looks 15 degrees to the right.

Colour uses the identity exposure (the average appearance of the scene). Depth is the rendered expected depth where
the accumulated opacity reaches --min-alpha (0 elsewhere), written at 5000 units per metre. The output folder is a
complete dataset: map it with DAAAM via `just gs-daaam <output>`.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np

from . import env
from .data import Camera, LayoutWriter, RGBDSequence, read_trajectory
from .geometry import invert, offset_in_camera_frame, path_length
from .metrics import color_correct, depth_metrics, psnr, ssim_map
from .model import load_checkpoint, render, sh_degree_of
from .train import OUTPUT_ROOT, colorize_depth


def parse_triplet(text: str, name: str) -> Tuple[float, float, float]:
	values = [float(v) for v in text.split(",")]
	if len(values) != 3:
		raise argparse.ArgumentTypeError(f"{name} needs three comma separated numbers, got {text!r}")
	return tuple(values)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
	p = argparse.ArgumentParser(description="Render an RGB-D dataset from a gs_world model.",
		formatter_class=argparse.ArgumentDefaultsHelpFormatter)
	p.add_argument("model", help="model folder (with model.pt), or 'latest'")
	p.add_argument("--out", type=Path, default=None, help="output dataset folder (default: <model>/renders/<trajectory name>)")
	p.add_argument("--overwrite", action="store_true")
	p.add_argument("--trajectory", default="original", help="original, test, or a trajectory file")
	p.add_argument("--offset", type=lambda t: parse_triplet(t, "--offset"), default=(0.0, 0.0, 0.0),
		help="shift X,Y,Z in metres in each camera's frame (x right, y down, z forward)")
	p.add_argument("--rotate", type=lambda t: parse_triplet(t, "--rotate"), default=(0.0, 0.0, 0.0),
		help="turn RX,RY,RZ in degrees about each camera's x, y, z axes")
	p.add_argument("--stride", type=int, default=1, help="render every N-th pose")
	p.add_argument("--max-frames", type=int, default=0, help="render at most this many poses (0: all)")
	p.add_argument("--scale", type=float, default=None,
		help="render at 1/scale of the dataset resolution (default: the training resolution)")
	p.add_argument("--min-alpha", type=float, default=0.7, help="write depth only where the opacity reaches this")
	p.add_argument("--max-depth", type=float, default=12.0, help="drop rendered depth beyond this distance (m)")
	p.add_argument("--exposure", choices=("identity", "nearest"), default="identity",
		help="identity: the scene's average appearance; nearest: the exposure the camera had at the nearest training "
		"frame (original/test trajectories only)")
	return p.parse_args(argv)


def resolve_model(spec: str) -> Path:
	if spec == "latest":
		models = sorted((OUTPUT_ROOT / "models").glob("*/*/model.pt"), key=lambda p: p.stat().st_mtime)
		if not models:
			raise SystemExit(f"no model under {OUTPUT_ROOT / 'models'}; train one with just gs-train")
		return models[-1].parent
	path = Path(spec)
	if path.is_file() and path.name == "model.pt":
		path = path.parent
	if not (path / "model.pt").is_file():
		raise SystemExit(f"{path} has no model.pt")
	return path


def trajectory_name(spec: str, offset, rotate) -> str:
	name = spec if spec in ("original", "test") else Path(spec).stem
	if any(offset):
		name += "_t" + "".join(f"{v:+.2f}" for v in offset)
	if any(rotate):
		name += "_r" + "".join(f"{v:+g}" for v in rotate)
	return name


def select_poses(meta: dict, args) -> Tuple[np.ndarray, np.ndarray, List[Optional[int]], float]:
	"""(T_world_camera [N, 4, 4], timestamps [N], source frame ids (None for file poses), fps) of the poses to render."""
	poses = np.asarray(meta["poses"], dtype=np.float64)
	stamps = np.asarray(meta["timestamps"], dtype=np.float64)
	fps = float(meta.get("fps") or 0.0) or 15.0
	if args.trajectory == "original":
		ids: List[Optional[int]] = list(range(len(poses)))
	elif args.trajectory == "test":
		ids = list(meta["test_ids"])
		if not ids:
			raise SystemExit("the model was trained without held-out frames (--test-every 0)")
	else:
		poses, times = read_trajectory(Path(args.trajectory))
		if len(poses) == 0:
			raise SystemExit(f"{args.trajectory} holds no pose")
		stamps = times if times is not None else np.arange(len(poses)) / fps
		return _limit(poses, stamps, [None] * len(poses), fps, args)
	return _limit(poses[ids], stamps[ids], ids, fps, args)


def _limit(poses, stamps, ids, fps, args):
	keep = np.arange(0, len(poses), max(1, args.stride))
	if args.max_frames and len(keep) > args.max_frames:
		keep = keep[: args.max_frames]
	return poses[keep], stamps[keep], [ids[i] for i in keep], fps / max(1, args.stride)


def summarize_comparison(rows: List[dict]) -> dict:
	"""Mean metrics of rendered against recorded frames, separately for held-out and training frames."""
	result = {}
	groups = (("held_out", [r for r in rows if r["held_out"]]), ("training", [r for r in rows if not r["held_out"]]))
	for group, members in groups:
		if members:
			keys = [k for k in members[0] if k not in ("frame", "held_out")]
			result[group] = {"frames": len(members), **{k: float(np.nanmean([r[k] for r in members])) for k in keys}}
	return result


def file_digest(path: Path) -> str:
	digest = hashlib.sha256()
	with open(path, "rb") as f:
		for block in iter(lambda: f.read(1 << 20), b""):
			digest.update(block)
	return digest.hexdigest()


def main(argv: Optional[Sequence[str]] = None) -> int:
	args = parse_args(argv)
	gsplat = env.import_gsplat()
	import torch

	model_dir = resolve_model(args.model)
	params, meta = load_checkpoint(model_dir / "model.pt", device="cuda")
	sh_degree = sh_degree_of(params)
	native = Camera.from_info(meta["camera"])
	scale = float(args.scale if args.scale is not None else meta.get("train_scale", 1.0))
	camera = native.scaled(scale)
	poses, stamps, ids, fps = select_poses(meta, args)
	poses = offset_in_camera_frame(poses, args.offset, args.rotate)
	name = trajectory_name(args.trajectory, args.offset, args.rotate)
	out = args.out or model_dir / "renders" / name
	source = (f"3DGS render ({name}) of {meta.get('dataset_source') or meta.get('dataset')}; model {model_dir}")
	writer = LayoutWriter(out, camera, fps, source, overwrite=args.overwrite, extra_info={"renderer": "gs_world"})
	print(f"model {model_dir}: {len(params['means'])} Gaussians, SH degree {sh_degree}, trained {meta.get('steps')} steps")
	print(f"rendering {len(poses)} poses ({name}) at {camera.width}x{camera.height} -> {out}")

	exposures = None
	if args.exposure == "nearest":
		if meta.get("exposure") is None or any(i is None for i in ids):
			raise SystemExit("--exposure nearest needs a model trained with exposure compensation and the original or "
				"test trajectory")
		train_array = np.asarray(meta["train_ids"])
		A = torch.tensor(meta["exposure"]["A"], dtype=torch.float32, device="cuda")
		b = torch.tensor(meta["exposure"]["b"], dtype=torch.float32, device="cuda")
		exposures = [int(np.argmin(np.abs(train_array - i))) for i in ids]

	# rendering the recorded poses themselves: compare every frame with the recorded one
	dataset = Path(str(meta.get("dataset", "")))
	compare = (args.trajectory in ("original", "test") and not any(args.offset) and not any(args.rotate)
		and (dataset / "rgb").is_dir())
	seq = RGBDSequence(dataset) if compare else None
	held_out = set(meta.get("test_ids") or [])
	recorded_valid = torch.from_numpy(seq.valid_mask(scale)).cuda() if compare else None
	rows = []

	K = torch.from_numpy(camera.gsplat_K()).float().cuda()[None]
	viewmats = torch.from_numpy(invert(poses)).float().cuda()
	coverage, depth_valid = [], []
	previews = []
	preview_at = set(np.linspace(0, len(poses) - 1, min(6, len(poses))).round().astype(int).tolist())
	tic = time.time()
	with torch.no_grad():
		for i in range(len(poses)):
			rgb, depth, alpha, _ = render(gsplat, params, viewmats[i][None], K, camera.width, camera.height, sh_degree)
			rgb = rgb[0]
			if exposures is not None:
				k = exposures[i]
				rgb = torch.einsum("hwc,dc->hwd", rgb, A[k]) + b[k]
			rgb_float = rgb.clamp(0, 1)
			rgb = (rgb_float * 255).round().byte().cpu().numpy()
			alpha = alpha[0].cpu().numpy()
			depth = depth[0].cpu().numpy()
			depth = np.where((alpha >= args.min_alpha) & (depth > 0) & (depth <= args.max_depth), depth, 0.0).astype(np.float32)
			if compare:
				frame = ids[i]
				recorded = torch.from_numpy(seq.rgb(frame, scale)).cuda().float() / 255.0
				row = {"frame": frame, "held_out": frame in held_out,
					"psnr": psnr(rgb_float, recorded, recorded_valid),
					"ssim": float(ssim_map(rgb_float, recorded)[recorded_valid].mean()),
					"cc_psnr": psnr(color_correct(rgb_float, recorded, recorded_valid).clamp(0, 1), recorded, recorded_valid)}
				if seq.has_depth:
					sensor = seq.depth(frame, scale)
					sensor[sensor > float(meta.get("max_depth", 5.0))] = 0.0
					row.update(depth_metrics(torch.from_numpy(depth).cuda(), torch.from_numpy(sensor).cuda(), recorded_valid))
				rows.append(row)
			writer.add(rgb, depth, poses[i], stamps[i])
			coverage.append(float((alpha >= args.min_alpha).mean()))
			depth_valid.append(float((depth > 0).mean()))
			if i in preview_at:
				previews.append(np.concatenate([cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), colorize_depth(depth, 6.0)], axis=0))
			if (i + 1) % 100 == 0:
				print(f"  {i + 1}/{len(poses)} frames, {(i + 1) / (time.time() - tic):.1f} fps", flush=True)
	elapsed = time.time() - tic
	out = writer.close()
	if previews:
		height = 240
		tiles = [cv2.resize(p, (int(p.shape[1] * height * 2 / p.shape[0]), height * 2)) for p in previews]
		cv2.imwrite(str(out / "preview.jpg"), np.concatenate(tiles, axis=1), [cv2.IMWRITE_JPEG_QUALITY, 88])
	report = {
		"model": str(model_dir),
		"model_sha256": file_digest(model_dir / "model.pt"),
		"dataset": meta.get("dataset"),
		"trajectory": args.trajectory,
		"offset_m": list(args.offset),
		"rotate_deg": list(args.rotate),
		"stride": args.stride,
		"frames": len(poses),
		"source_frames": ids,
		"camera": camera.info(),
		"fps": fps,
		"min_alpha": args.min_alpha,
		"max_depth_m": args.max_depth,
		"path_length_m": round(path_length(poses), 3),
		"mean_coverage": round(float(np.mean(coverage)), 4),
		"mean_depth_valid": round(float(np.mean(depth_valid)), 4),
		"exposure": args.exposure,
		"comparison_with_recorded_frames": summarize_comparison(rows) if rows else None,
		"render_seconds": round(elapsed, 1),
		"created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
	}
	(out / "gs_render.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
	print(f"wrote {len(poses)} frames in {elapsed:.1f} s ({len(poses) / max(elapsed, 1e-6):.1f} fps); "
		f"mean coverage {report['mean_coverage']:.1%}, depth on {report['mean_depth_valid']:.1%} of the pixels")
	for group, values in (report["comparison_with_recorded_frames"] or {}).items():
		print(f"vs recorded {group} frames ({values['frames']}): "
			+ "  ".join(f"{k} {v:.4f}" for k, v in values.items() if k != "frames"))
	print(f"dataset: {out}\npreview: {out / 'preview.jpg'}\nmap it with DAAAM: just gs-daaam {out}")
	return 0


if __name__ == "__main__":
	sys.exit(main())
