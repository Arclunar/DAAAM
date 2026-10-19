"""Gaussian parameters: initialisation from posed RGB-D, rendering, checkpoints and PLY export.

Parameters are stored the way gsplat's strategies expect them: means [N, 3], log scales [N, 3], quaternions
(w, x, y, z; normalised by the rasteriser) [N, 4], logit opacities [N], SH DC [N, 1, 3] and higher SH bands
[N, K, 3]. gsplat itself is only imported by render(); everything else works on CPU without it.
"""

import os
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

import numpy as np
import torch

from .data import Camera, RGBDSequence, filter_depth_edges

SH_C0 = 0.28209479177387814
PARAM_KEYS = ("means", "scales", "quats", "opacities", "sh0", "shN")
FORMAT = "daaam-gs-world/1"


def backproject(depth_m: np.ndarray, camera: Camera, T_world_camera: np.ndarray, stride: int = 1,
		rgb: Optional[np.ndarray] = None, valid: Optional[np.ndarray] = None) -> Tuple[np.ndarray, Optional[np.ndarray]]:
	"""World points (and their colours in [0, 1]) of every stride-th pixel with a depth measurement."""
	h, w = depth_m.shape
	v, u = np.mgrid[0:h:stride, 0:w:stride]
	d = depth_m[v, u]
	keep = d > 0
	if valid is not None:
		keep &= valid[v, u]
	u, v, d = u[keep].astype(np.float64), v[keep].astype(np.float64), d[keep].astype(np.float64)
	K = camera.K
	x = (u - K[0, 2]) / K[0, 0] * d
	y = (v - K[1, 2]) / K[1, 1] * d
	points = np.stack([x, y, d], axis=1) @ T_world_camera[:3, :3].T + T_world_camera[:3, 3]
	colors = rgb[v.astype(int), u.astype(int)].astype(np.float32) / 255.0 if rgb is not None else None
	return points.astype(np.float32), colors


def voxel_downsample(points: np.ndarray, colors: np.ndarray, voxel: float) -> Tuple[np.ndarray, np.ndarray]:
	"""Average the points (and colours) that fall into the same voxel."""
	keys = np.floor(points / voxel).astype(np.int64) + (1 << 20)
	if keys.min() < 0 or keys.max() >= (1 << 21):
		raise ValueError("scene too large for the voxel hash (more than 10 000 voxels from the origin)")
	flat = (keys[:, 0] << 42) | (keys[:, 1] << 21) | keys[:, 2]
	_, inverse, counts = np.unique(flat, return_inverse=True, return_counts=True)
	inverse = inverse.reshape(-1)
	out_points = np.stack([np.bincount(inverse, points[:, c], len(counts)) for c in range(3)], 1) / counts[:, None]
	out_colors = np.stack([np.bincount(inverse, colors[:, c], len(counts)) for c in range(3)], 1) / counts[:, None]
	return out_points.astype(np.float32), out_colors.astype(np.float32)


def initial_points(seq: RGBDSequence, frame_ids: Iterable[int], scale: float = 1.0, voxel: float = 0.01,
		max_points: int = 300_000, max_depth: float = 8.0, edge_jump: float = 0.1, max_frames: int = 150,
		valid: Optional[np.ndarray] = None, fill_missing: bool = True,
		seed: int = 0) -> Tuple[np.ndarray, np.ndarray]:
	"""Coloured point cloud fused from the depth of up to max_frames of the given frames.

	Pixels without a usable measurement (glass, sunlight, beyond max_depth) would otherwise get no Gaussian at all and
	stay black; with fill_missing they get a sparse layer of points at max_depth along their viewing rays, which the
	colour loss then moves and shapes.
	"""
	if not seq.has_depth:
		raise ValueError(f"{seq.root} has no depth/ folder: gs_world initialises the Gaussians from RGB-D depth")
	frame_ids = list(frame_ids)
	if len(frame_ids) > max_frames:
		frame_ids = [frame_ids[i] for i in np.linspace(0, len(frame_ids) - 1, max_frames).round().astype(int)]
	camera = seq.camera.scaled(scale)
	stride = max(1, int(round(np.sqrt(camera.width * camera.height / 20000.0))))  # about 20 000 samples per frame
	all_points, all_colors = [], []
	for i in frame_ids:
		raw = seq.depth(i, scale)
		rgb = seq.rgb(i, scale)
		depth = np.where(raw > max_depth, 0.0, raw).astype(np.float32)
		if edge_jump > 0:
			depth = filter_depth_edges(depth, edge_jump)
		points, colors = backproject(depth, camera, seq.poses[i], stride, rgb, valid)
		all_points.append(points)
		all_colors.append(colors)
		if fill_missing:
			missing = (raw <= 0) | (raw > max_depth)  # pixels dropped as depth edges keep no fill: they lie next to real depth
			fill = np.where(missing, np.float32(max_depth), np.float32(0.0))
			points, colors = backproject(fill, camera, seq.poses[i], 2 * stride, rgb, valid)
			all_points.append(points)
			all_colors.append(colors)
	points = np.concatenate(all_points) if all_points else np.zeros((0, 3), np.float32)
	colors = np.concatenate(all_colors) if all_colors else np.zeros((0, 3), np.float32)
	if len(points) == 0:
		raise ValueError("no valid depth measurement to initialise the Gaussians")
	points, colors = voxel_downsample(points, colors, voxel)
	if len(points) > max_points:
		pick = np.random.default_rng(seed).choice(len(points), max_points, replace=False)
		points, colors = points[pick], colors[pick]
	return points, colors


def knn_mean_distance(points: np.ndarray, k: int = 3) -> np.ndarray:
	"""Root mean square distance of every point to its k nearest neighbours."""
	from scipy.spatial import cKDTree

	if len(points) <= k:
		return np.full(len(points), 0.01, dtype=np.float32)
	distances, _ = cKDTree(points).query(points, k=k + 1)
	return np.sqrt(np.mean(distances[:, 1:] ** 2, axis=1)).astype(np.float32)


def create_params(points: np.ndarray, colors: np.ndarray, sh_degree: int = 3, init_opacity: float = 0.5,
		init_scale: float = 0.5, device: str = "cuda") -> torch.nn.ParameterDict:
	"""Isotropic Gaussians at the points, sized init_scale x their neighbour spacing."""
	n = len(points)
	spacing = np.maximum(knn_mean_distance(points) * init_scale, 1e-4)
	sh = np.zeros((n, (sh_degree + 1) ** 2, 3), np.float32)
	sh[:, 0, :] = (np.clip(colors, 0.0, 1.0) - 0.5) / SH_C0
	quats = np.zeros((n, 4), np.float32)
	quats[:, 0] = 1.0
	tensors = {
		"means": torch.from_numpy(np.ascontiguousarray(points, dtype=np.float32)),
		"scales": torch.from_numpy(np.log(spacing))[:, None].repeat(1, 3),
		"quats": torch.from_numpy(quats),
		"opacities": torch.logit(torch.full((n,), float(init_opacity))),
		"sh0": torch.from_numpy(sh[:, :1, :]),
		"shN": torch.from_numpy(sh[:, 1:, :]),
	}
	return torch.nn.ParameterDict({k: torch.nn.Parameter(v.to(device)) for k, v in tensors.items()})


def render(gsplat, params, viewmats: torch.Tensor, Ks: torch.Tensor, width: int, height: int, sh_degree: int,
		with_depth: bool = True, near: float = 0.01, far: float = 1000.0, backgrounds: Optional[torch.Tensor] = None):
	"""Render C views -> (rgb [C, H, W, 3], expected depth [C, H, W] or None, alpha [C, H, W], info)."""
	colors = torch.cat([params["sh0"], params["shN"]], dim=1)
	renders, alphas, info = gsplat.rasterization(
		means=params["means"],
		quats=params["quats"],
		scales=torch.exp(params["scales"]),
		opacities=torch.sigmoid(params["opacities"]),
		colors=colors,
		viewmats=viewmats,
		Ks=Ks,
		width=width,
		height=height,
		near_plane=near,
		far_plane=far,
		sh_degree=sh_degree,
		packed=False,
		backgrounds=backgrounds,
		render_mode="RGB+ED" if with_depth else "RGB",
		rasterize_mode="antialiased",
	)
	rgb = renders[..., :3]
	depth = renders[..., 3] if with_depth else None
	return rgb, depth, alphas[..., 0], info


def save_checkpoint(path: Path, params, meta: Dict) -> None:
	"""model.pt: CPU copies of the parameters plus the metadata (written atomically)."""
	path = Path(path)
	temporary = path.with_name(path.name + ".tmp")
	torch.save({"format": FORMAT, "params": {k: params[k].detach().cpu() for k in PARAM_KEYS}, "meta": meta}, temporary)
	os.replace(temporary, path)


def load_checkpoint(path: Path, device: str = "cuda") -> Tuple[Dict[str, torch.Tensor], Dict]:
	data = torch.load(path, map_location="cpu", weights_only=False)
	if data.get("format") != FORMAT:
		raise ValueError(f"{path} is not a gs_world checkpoint (format {data.get('format')!r})")
	params = {k: data["params"][k].to(device) for k in PARAM_KEYS}
	return params, data["meta"]


def sh_degree_of(params) -> int:
	k = 1 + params["shN"].shape[1]
	degree = int(round(np.sqrt(k))) - 1
	if (degree + 1) ** 2 != k:
		raise ValueError(f"{k} SH coefficients do not form a complete set of bands")
	return degree


def write_ply(path: Path, params) -> int:
	"""Write the Gaussians in the PLY layout of the original 3DGS code (read by SuperSplat and most splat viewers).

	Fields per vertex: x y z, nx ny nz (zero), f_dc_0..2, f_rest_* (channel-major), opacity (logit), scale_0..2 (log),
	rot_0..3 (w, x, y, z, normalised). Returns the number of Gaussians written (non-finite ones are skipped).
	"""
	means = params["means"].detach().cpu().numpy().astype(np.float32)
	n = len(means)
	sh0 = params["sh0"].detach().cpu().numpy().reshape(n, 3).astype(np.float32)
	shn = params["shN"].detach().cpu().numpy().astype(np.float32)
	rest = np.transpose(shn, (0, 2, 1)).reshape(n, -1)
	opacity = params["opacities"].detach().cpu().numpy().reshape(n, 1).astype(np.float32)
	scales = params["scales"].detach().cpu().numpy().astype(np.float32)
	quats = params["quats"].detach().cpu().numpy().astype(np.float32)
	quats = quats / np.maximum(np.linalg.norm(quats, axis=1, keepdims=True), 1e-12)
	columns = np.concatenate([means, np.zeros_like(means), sh0, rest, opacity, scales, quats], axis=1)
	columns = columns[np.all(np.isfinite(columns), axis=1)]
	names = (["x", "y", "z", "nx", "ny", "nz", "f_dc_0", "f_dc_1", "f_dc_2"]
		+ [f"f_rest_{i}" for i in range(rest.shape[1])]
		+ ["opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"])
	header = ["ply", "format binary_little_endian 1.0", f"element vertex {len(columns)}"]
	header += [f"property float {name}" for name in names] + ["end_header"]
	path = Path(path)
	temporary = path.with_name(path.name + ".tmp")
	with open(temporary, "wb") as f:
		f.write(("\n".join(header) + "\n").encode("ascii"))
		f.write(np.ascontiguousarray(columns, dtype="<f4").tobytes())
	os.replace(temporary, path)
	return len(columns)


def read_ply(path: Path) -> Dict[str, np.ndarray]:
	"""Read back a PLY written by write_ply as {field name: values}."""
	with open(path, "rb") as f:
		names, count = [], 0
		while True:
			line = f.readline().decode("ascii").strip()
			if line.startswith("element vertex"):
				count = int(line.split()[-1])
			elif line.startswith("property float"):
				names.append(line.split()[-1])
			elif line == "end_header":
				break
		values = np.frombuffer(f.read(), dtype="<f4").reshape(count, len(names))
	return {name: values[:, i] for i, name in enumerate(names)}
