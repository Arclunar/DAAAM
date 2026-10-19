"""Train a 3D Gaussian splatting model on a posed RGB-D folder with gsplat.

	docker compose run --rm -T daaam-ws python -m scripts.gs_world.train tum_fr1_room       (just gs-train fr1_room)

The recipe is the most robust published combination that gsplat implements, plus what RGB-D input adds:
- 3DGS-MCMC densification (Kheradmand et al., NeurIPS 2024): a fixed budget of Gaussians (--cap-max) and relocation of
  dead Gaussians instead of split/clone heuristics, so the memory use is known in advance;
- anti-aliased rasterisation (the Mip-Splatting 2D filter), stable when rendered at other distances or resolutions;
- initialisation from the fused sensor depth instead of SfM points, and an L1 loss on the measured depth (flying
  pixels at depth edges and depth beyond --max-depth are ignored);
- a per-image affine colour transform that absorbs auto-exposure and white-balance changes; renders use the identity,
  i.e. the average appearance of the scene;
- optional per-image pose refinement (--pose-opt) for poses from odometry rather than motion capture.

Every --test-every-th frame is held out and evaluated at the end (PSNR, SSIM, colour-corrected PSNR, depth errors).
Output folder (default /data/gs_world/models/<dataset>/<UTC time>): model.pt (parameters and metadata for
gs_world.render), point_cloud.ply (3DGS PLY for splat viewers), metrics.json, train_log.csv, config.json, previews/.
"""

import argparse
import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import cv2
import numpy as np

from . import env
from .data import RGBDSequence, filter_depth_edges, resolve_dataset
from .geometry import scene_extent
from .metrics import color_correct, depth_metrics, psnr, ssim_map
from .model import create_params, initial_points, render, save_checkpoint, write_ply

OUTPUT_ROOT = Path(os.environ.get("GS_OUTPUT_ROOT", "/data/gs_world"))


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
	p = argparse.ArgumentParser(description="Train a 3DGS model (gsplat, MCMC) on a posed RGB-D folder.",
		formatter_class=argparse.ArgumentDefaultsHelpFormatter)
	p.add_argument("data", help="dataset folder, or a name under /datasets (tum_fr1_room or fr1_room)")
	p.add_argument("--out", type=Path, default=None, help="output folder (default: /data/gs_world/models/<dataset>/<UTC time>)")
	p.add_argument("--overwrite", action="store_true", help="write into an existing non-empty output folder")
	p.add_argument("--iterations", type=int, default=30000)
	p.add_argument("--downscale", default="auto",
		help="train on images resized to 1/N; auto: the smallest integer N that brings the longer side to at most 1000 px")
	p.add_argument("--test-every", type=int, default=8, help="hold out every N-th frame for evaluation (0: train on all frames)")
	p.add_argument("--sh-degree", type=int, default=3, help="spherical harmonics degree of the colours")
	p.add_argument("--cap-max", type=int, default=1_000_000, help="maximum number of Gaussians (the MCMC budget)")
	p.add_argument("--init-points", type=int, default=300_000, help="initial Gaussians sampled from the fused depth")
	p.add_argument("--init-voxel", type=float, default=0.01, help="voxel size (m) for fusing the initial point cloud")
	p.add_argument("--init-opacity", type=float, default=0.5)
	p.add_argument("--init-scale", type=float, default=0.5, help="initial Gaussian size relative to the point spacing")
	p.add_argument("--ssim-weight", type=float, default=0.2, help="D-SSIM share of the photometric loss")
	p.add_argument("--depth-weight", type=float, default=0.3, help="weight of the L1 depth loss in metres (0: colour only)")
	p.add_argument("--max-depth", type=float, default=5.0, help="supervise with measured depth up to this distance (m)")
	p.add_argument("--init-max-depth", type=float, default=8.0,
		help="initialise from measured depth up to this distance (m); pixels without depth get a sparse layer of points here")
	p.add_argument("--no-fill", action="store_true", help="do not seed Gaussians where the sensor measured no depth")
	p.add_argument("--depth-edge", type=float, default=0.1,
		help="ignore depth pixels whose 3x3 neighbourhood jumps by more than this fraction of their depth (0: keep all)")
	p.add_argument("--no-exposure", action="store_true", help="disable the per-image exposure compensation")
	p.add_argument("--pose-opt", action="store_true", help="refine the training poses (use for odometry poses)")
	p.add_argument("--opacity-reg", type=float, default=0.01, help="MCMC opacity regulariser")
	p.add_argument("--scale-reg", type=float, default=0.01, help="MCMC scale regulariser")
	p.add_argument("--eval-every", type=int, default=0, help="also evaluate the test frames every N steps (0: only at the end)")
	p.add_argument("--log-every", type=int, default=200)
	p.add_argument("--seed", type=int, default=0)
	return p.parse_args(argv)


def downscale_factor(spec: str, width: int, height: int) -> float:
	if str(spec) == "auto":
		return float(max(1, math.ceil(max(width, height) / 1000.0)))
	factor = float(spec)
	if factor < 1:
		raise SystemExit("--downscale must be at least 1")
	return factor


def split_frames(n: int, test_every: int):
	"""(train ids, test ids): every test_every-th frame (starting with frame 0) is a test frame."""
	if test_every <= 0 or n < 2 * test_every:
		return list(range(n)), []
	test = list(range(0, n, test_every))
	held = set(test)
	return [i for i in range(n) if i not in held], test


def colorize_depth(depth: np.ndarray, max_depth: float) -> np.ndarray:
	"""Depth in metres as a BGR turbo image (black where there is no depth)."""
	scaled = np.clip(depth / max_depth * 255.0, 0, 255).astype(np.uint8)
	image = cv2.applyColorMap(scaled, cv2.COLORMAP_TURBO)
	image[depth <= 0] = 0
	return image


def main(argv: Optional[Sequence[str]] = None) -> int:
	args = parse_args(argv)
	if args.iterations < 1:
		raise SystemExit("--iterations must be at least 1")
	gsplat = env.import_gsplat()
	import torch

	# Per-image parameters live in sparse embeddings optimised with SparseAdam: only the row of the image of the
	# current step moves (a dense Adam would keep moving the other images' rows with its momentum).
	class Exposure(torch.nn.Module):
		"""Per-image affine colour transform rgb @ A^T + b (initialised to the identity)."""

		def __init__(self, n: int):
			super().__init__()
			self.table = torch.nn.Embedding(n, 12, sparse=True)
			with torch.no_grad():
				self.table.weight.zero_()
				self.table.weight[:, [0, 4, 8]] = 1.0

		def row(self, k: int):
			return self.table(torch.tensor([k], device=self.table.weight.device))[0]

		def forward(self, rgb, k: int):
			row = self.row(k)
			A, b = row[:9].reshape(3, 3), row[9:]
			penalty = ((A - torch.eye(3, device=A.device)) ** 2).sum() + (b ** 2).sum()
			return torch.einsum("hwc,dc->hwd", rgb, A) + b, penalty

		def matrices(self):
			weight = self.table.weight.detach().cpu()
			return weight[:, :9].reshape(-1, 3, 3), weight[:, 9:]

	class PoseCorrection(torch.nn.Module):
		"""Per-image correction T_world_camera' = T_world_camera @ [R(phi) | rho], rho/phi learned (zero at start)."""

		def __init__(self, n: int):
			super().__init__()
			self.table = torch.nn.Embedding(n, 6, sparse=True)
			with torch.no_grad():
				self.table.weight.zero_()

		def forward(self, k: int):
			delta = self.table(torch.tensor([k], device=self.table.weight.device))[0]
			rho, phi = delta[:3], delta[3:]
			theta = torch.sqrt((phi ** 2).sum() + 1e-12)
			zero = torch.zeros((), device=phi.device)
			skew = torch.stack([zero, -phi[2], phi[1], phi[2], zero, -phi[0], -phi[1], phi[0], zero]).reshape(3, 3)
			R = (torch.eye(3, device=phi.device) + torch.sin(theta) / theta * skew
				+ (1 - torch.cos(theta)) / theta ** 2 * (skew @ skew))
			top = torch.cat([R, rho[:, None]], dim=1)
			T = torch.cat([top, torch.tensor([[0.0, 0.0, 0.0, 1.0]], device=phi.device)], dim=0)
			return T, (delta ** 2).sum()

	torch.manual_seed(args.seed)
	rng = np.random.default_rng(args.seed)
	if not torch.cuda.is_available():
		raise SystemExit("CUDA is not available")
	device = "cuda"

	data_dir = resolve_dataset(args.data)
	seq = RGBDSequence(data_dir)
	scale = downscale_factor(args.downscale, seq.camera.width, seq.camera.height)
	cam = seq.camera.scaled(scale)
	n = len(seq)
	train_ids, test_ids = split_frames(n, args.test_every)
	out = args.out or OUTPUT_ROOT / "models" / data_dir.name / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
	if out.exists() and any(out.iterdir()) and not args.overwrite:
		raise SystemExit(f"{out} is not empty; choose another --out or pass --overwrite")
	(out / "previews").mkdir(parents=True, exist_ok=True)
	config = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()}
	config.update({"dataset": str(data_dir), "downscale_factor": scale, "train_frames": len(train_ids),
		"test_frames": len(test_ids), "gsplat": gsplat.__version__, "torch": torch.__version__})
	(out / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
	print(f"dataset {data_dir} ({seq.source or 'no source'}): {n} frames, {seq.camera.width}x{seq.camera.height}, "
		f"training at {cam.width}x{cam.height} (1/{scale:g}); {len(train_ids)} train / {len(test_ids)} test frames")
	print(f"output {out}")

	# all frames in host memory: uint8 colour, float16 metric depth (flying pixels and far depth removed)
	tic = time.time()
	valid = seq.valid_mask(scale)
	rgb_cpu = torch.empty((n, cam.height, cam.width, 3), dtype=torch.uint8)
	depth_cpu = torch.zeros((n, cam.height, cam.width), dtype=torch.float16) if seq.has_depth else None
	for i in range(n):
		rgb_cpu[i] = torch.from_numpy(seq.rgb(i, scale))
		if depth_cpu is not None:
			d = seq.depth(i, scale)
			d[d > args.max_depth] = 0.0
			if args.depth_edge > 0:
				d = filter_depth_edges(d, args.depth_edge)
			depth_cpu[i] = torch.from_numpy(d.astype(np.float16))
	if depth_cpu is None and args.depth_weight > 0:
		print("warning: the dataset has no depth; training on colour only")
	print(f"loaded {n} frames in {time.time() - tic:.1f} s; {valid.mean():.1%} of the pixels hold image content")

	tic = time.time()
	points, colors = initial_points(seq, train_ids, scale, voxel=args.init_voxel, max_points=args.init_points,
		max_depth=args.init_max_depth, edge_jump=args.depth_edge, valid=valid, fill_missing=not args.no_fill,
		seed=args.seed)
	params = create_params(points, colors, args.sh_degree, args.init_opacity, args.init_scale, device)
	extent = scene_extent(seq.poses[train_ids])
	print(f"initialised {len(points)} Gaussians from depth in {time.time() - tic:.1f} s; scene extent {extent:.2f} m")

	lrs = {"means": 1.6e-4 * extent, "scales": 5e-3, "quats": 1e-3, "opacities": 5e-2, "sh0": 2.5e-3,
		"shN": 2.5e-3 / 20}
	optimizers = {k: torch.optim.Adam([{"params": params[k], "lr": lr, "name": k}], eps=1e-15) for k, lr in lrs.items()}
	scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizers["means"], gamma=0.01 ** (1.0 / args.iterations))
	strategy = gsplat.MCMCStrategy(cap_max=args.cap_max, noise_lr=5e5, refine_start_iter=500,
		refine_stop_iter=int(args.iterations * 25 / 30), refine_every=100, min_opacity=0.005)
	strategy.check_sanity(params, optimizers)
	strategy_state = strategy.initialize_state()

	train_index = {frame: k for k, frame in enumerate(train_ids)}
	exposure = None if args.no_exposure else Exposure(len(train_ids)).to(device)
	exposure_opt = torch.optim.SparseAdam(list(exposure.parameters()), lr=2.5e-3) if exposure is not None else None
	poses_fix = PoseCorrection(len(train_ids)).to(device) if args.pose_opt else None
	pose_opt = torch.optim.SparseAdam(list(poses_fix.parameters()), lr=1e-4) if poses_fix is not None else None

	c2w = torch.from_numpy(seq.poses).float().to(device)
	viewmats = torch.linalg.inv(c2w)
	K = torch.from_numpy(cam.gsplat_K()).float().to(device)[None]
	valid_t = torch.from_numpy(valid).to(device)
	preview_ids = set((test_ids or train_ids)[j] for j in np.linspace(0, len(test_ids or train_ids) - 1,
		min(4, len(test_ids or train_ids))).round().astype(int))

	train_array = np.asarray(train_ids)

	@torch.no_grad()
	def evaluate(frames: List[int], previews: bool) -> Dict:
		"""psnr/ssim use the identity exposure (the renders' appearance); psnr_exposure applies the exposure of the
		nearest training frame (the camera's exposure at that moment); cc_psnr fits the best colour transform."""
		rows = []
		for frame in frames:
			raw, depth, alpha, _ = render(gsplat, params, viewmats[frame][None], K, cam.width, cam.height,
				args.sh_degree, with_depth=True)
			rgb, depth, alpha = raw[0].clamp(0, 1), depth[0], alpha[0]
			gt = rgb_cpu[frame].to(device).float() / 255.0
			row = {"frame": int(frame), "psnr": psnr(rgb, gt, valid_t), "ssim": float(ssim_map(rgb, gt)[valid_t].mean()),
				"cc_psnr": psnr(color_correct(rgb, gt, valid_t).clamp(0, 1), gt, valid_t),
				"coverage": float((alpha > 0.5)[valid_t].float().mean())}
			if exposure is not None:
				nearest = int(np.argmin(np.abs(train_array - frame)))
				row["psnr_exposure"] = psnr(exposure(raw[0], nearest)[0].clamp(0, 1), gt, valid_t)
			if depth_cpu is not None:
				gt_depth = depth_cpu[frame].to(device).float()
				row.update(depth_metrics(depth, gt_depth, valid_t & (alpha > 0.5)))
			rows.append(row)
			if previews and frame in preview_ids:
				panels = [cv2.cvtColor((gt.cpu().numpy() * 255).astype(np.uint8), cv2.COLOR_RGB2BGR),
					cv2.cvtColor((rgb.cpu().numpy() * 255).astype(np.uint8), cv2.COLOR_RGB2BGR),
					colorize_depth(np.where(alpha.cpu().numpy() > 0.5, depth.cpu().numpy(), 0), args.max_depth)]
				if depth_cpu is not None:
					panels.append(colorize_depth(depth_cpu[frame].float().numpy(), args.max_depth))
				cv2.imwrite(str(out / "previews" / f"frame_{frame:06d}.jpg"), np.concatenate(panels, axis=1),
					[cv2.IMWRITE_JPEG_QUALITY, 90])
		keys = [k for k in rows[0] if k != "frame"] if rows else []
		summary = {k: float(np.nanmean([r[k] for r in rows])) for k in keys}
		return {"frames": len(rows), "mean": summary, "per_frame": rows}

	log_path = out / "train_log.csv"
	log_file = open(log_path, "w", newline="", encoding="utf-8")
	log = csv.writer(log_file)
	log.writerow(["step", "loss", "l1", "ssim", "depth_l1_m", "psnr", "gaussians", "lr_means", "seconds"])
	order = rng.permutation(train_ids)
	pointer = 0
	start = time.time()
	step = 0
	interrupted = False
	evals = []
	try:
		for step in range(args.iterations):
			if pointer >= len(order):
				order, pointer = rng.permutation(train_ids), 0
			frame = int(order[pointer])
			pointer += 1
			k = train_index[frame]
			gt = rgb_cpu[frame].to(device).float() / 255.0
			pose_penalty = exposure_penalty = None
			if poses_fix is None:
				viewmat = viewmats[frame]
			else:
				correction, pose_penalty = poses_fix(k)
				viewmat = torch.linalg.inv(c2w[frame] @ correction)
			sh_degree = min(step // 1000, args.sh_degree)
			rgb, depth, alpha, info = render(gsplat, params, viewmat[None], K, cam.width, cam.height, sh_degree,
				with_depth=depth_cpu is not None)
			rgb = rgb[0]
			if exposure is not None:
				rgb, exposure_penalty = exposure(rgb, k)
			l1 = (rgb - gt).abs()[valid_t].mean()
			ssim = ssim_map(rgb, gt)[valid_t].mean()
			loss = (1.0 - args.ssim_weight) * l1 + args.ssim_weight * (1.0 - ssim)
			depth_l1 = None
			if depth_cpu is not None and args.depth_weight > 0:
				gt_depth = depth_cpu[frame].to(device).float()
				mask = valid_t & (gt_depth > 0)
				if bool(mask.any()):
					depth_l1 = (depth[0] - gt_depth).abs()[mask].mean()
					loss = loss + args.depth_weight * depth_l1
			if args.opacity_reg > 0:
				loss = loss + args.opacity_reg * torch.sigmoid(params["opacities"]).mean()
			if args.scale_reg > 0:
				loss = loss + args.scale_reg * torch.exp(params["scales"]).mean()
			if exposure_penalty is not None:
				loss = loss + 1e-3 * exposure_penalty
			if pose_penalty is not None:
				loss = loss + 1e-6 * pose_penalty
			loss.backward()
			for optimizer in optimizers.values():
				optimizer.step()
				optimizer.zero_grad(set_to_none=True)
			for optimizer in (exposure_opt, pose_opt):
				if optimizer is not None:
					optimizer.step()
					optimizer.zero_grad(set_to_none=True)
			scheduler.step()
			strategy.step_post_backward(params, optimizers, strategy_state, step, info, lr=scheduler.get_last_lr()[0])

			done = step + 1
			if done % args.log_every == 0 or done == args.iterations:
				elapsed = time.time() - start
				with torch.no_grad():
					train_psnr = psnr(rgb.detach().clamp(0, 1), gt, valid_t)
				depth_value = float(depth_l1.detach()) if depth_l1 is not None else float("nan")
				loss_value, l1_value, ssim_value = float(loss.detach()), float(l1.detach()), float(ssim.detach())
				log.writerow([done, f"{loss_value:.6f}", f"{l1_value:.6f}", f"{ssim_value:.6f}", f"{depth_value:.6f}",
					f"{train_psnr:.3f}", len(params["means"]), f"{scheduler.get_last_lr()[0]:.3e}", f"{elapsed:.1f}"])
				log_file.flush()
				rate = done / max(elapsed, 1e-6)
				eta = (args.iterations - done) / max(rate, 1e-6)
				print(f"step {done:6d}/{args.iterations}  loss {loss_value:.4f}  l1 {l1_value:.4f}  depth {depth_value:.4f} m"
					f"  psnr {train_psnr:5.2f}  gaussians {len(params['means']):8d}  {rate:5.1f} it/s  eta {eta / 60:5.1f} min",
					flush=True)
			if args.eval_every and done % args.eval_every == 0 and done < args.iterations and test_ids:
				result = evaluate(test_ids, previews=False)
				evals.append({"step": done, **result["mean"]})
				print(f"eval step {done}: " + "  ".join(f"{k} {v:.4f}" for k, v in result["mean"].items()), flush=True)
	except KeyboardInterrupt:
		interrupted = True
		print(f"interrupted at step {step}; saving the current model", flush=True)
	finally:
		log_file.close()
	train_seconds = time.time() - start
	steps_done = step + (0 if interrupted else 1)

	final = evaluate(test_ids if test_ids else train_ids[:: max(1, len(train_ids) // 50)], previews=True)
	peak_gb = torch.cuda.max_memory_allocated() / 2 ** 30
	meta = {
		"dataset": str(data_dir),
		"dataset_source": seq.source,
		"camera": seq.camera.info(),
		"train_scale": scale,
		"train_camera": cam.info(),
		"fps": seq.fps,
		"poses": seq.poses.tolist(),
		"timestamps": seq.timestamps.tolist(),
		"train_ids": train_ids,
		"test_ids": test_ids,
		"sh_degree": args.sh_degree,
		"steps": steps_done,
		"interrupted": interrupted,
		"max_depth": args.max_depth,
		"exposure": None if exposure is None else dict(zip(("A", "b"), (t.tolist() for t in exposure.matrices()))),
		"pose_corrections": None if poses_fix is None else poses_fix.table.weight.detach().cpu().tolist(),
		"config": config,
		"created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
	}
	save_checkpoint(out / "model.pt", params, meta)
	written = write_ply(out / "point_cloud.ply", params)
	metrics = {
		"split": "test" if test_ids else "train (no held-out frames)",
		**final,
		"periodic": evals,
		"gaussians": len(params["means"]),
		"steps": steps_done,
		"train_seconds": round(train_seconds, 1),
		"peak_gpu_memory_gb": round(peak_gb, 2),
	}
	(out / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
	mean = final["mean"]
	print(f"\ndone: {steps_done} steps in {train_seconds / 60:.1f} min, {written} Gaussians, peak GPU memory {peak_gb:.1f} GB")
	print(f"{metrics['split']} frames ({final['frames']}): " + "  ".join(f"{k} {v:.4f}" for k, v in mean.items()))
	print(f"model: {out / 'model.pt'}\nPLY for splat viewers: {out / 'point_cloud.ply'}\n"
		f"previews (image | render | rendered depth | measured depth): {out / 'previews'}")
	print(f"render the recorded trajectory as an RGB-D dataset: just gs-render {out}")
	return 130 if interrupted else 0


if __name__ == "__main__":
	sys.exit(main())
