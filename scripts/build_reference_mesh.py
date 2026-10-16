#!/usr/bin/env python3
"""Build, cache and reuse a 1 cm reference mesh of a prepared RGB-D dataset with an Open3D TSDF.

  python scripts/build_reference_mesh.py DATASET [--cache-dir DIR] [--force]

DATASET is a folder in the layout that scripts/prepare_tum_rgbd.py writes (rgb/, depth/, pose/poses.txt, timestamps.txt,
camera_info.json). Every frame is integrated once, whatever number of frames a replay run used, into a CPU
`ScalableTSDFVolume` (voxel 1 cm, truncation 4 cm, depth up to 4 m); the surface is extracted, cleaned (duplicate and
degenerate triangles, unreferenced vertices) and stored with normals, never decimated. The poses are `T_world_camera`
(row-major 4x4 per line), so each one is inverted for Open3D. The result is a *reference* mesh, not ground truth: it is
reconstructed from noisy depth images, calibration, interpolated poses and the TSDF parameters.

The cache is <DATASET>/.daaam/reference_mesh/open3d_tsdf_1cm.npz (arrays positions float32, colors uint8, normals
float32, triangles uint32) next to open3d_tsdf_1cm.json (the manifest: fingerprint, parameters, frame counts, bounds,
versions). --cache-dir or $REFERENCE_MESH_CACHE puts both files in another directory, for a read-only dataset; use one
directory per dataset. The fingerprint covers the camera, pose and timestamp files, the ordered rgb/depth file list with
sizes and modification times, and the reconstruction parameters: any change of them makes the cache stale, a valid cache
is reused without importing Open3D, --force rebuilds it. Files are published atomically (temporary file + rename).

Only the path of the cache goes to stdout; progress and warnings go to stderr, so a shell script can capture the path:
  reference=$(python scripts/build_reference_mesh.py /datasets/tum_fr1_desk)
Open3D is a large wheel (about 450 MB): pip install open3d==0.19.0.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.abc
import importlib.machinery
import json
import os
import platform
import sys
import time
import types
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

CACHE_SCHEMA = 1
CACHE_STEM = "open3d_tsdf_1cm"
POSE_CONVENTION = "T_world_camera"
RGB_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp")
DEPTH_SUFFIXES = (".png", ".tif", ".tiff")
ARRAYS = {"positions": ("float32", 3), "colors": ("uint8", 3), "normals": ("float32", 3), "triangles": ("uint32", 3)}
SKIPPED_WARNING_RATIO = 0.1
INSTALL_HINT = "install it with `pip install open3d==0.19.0` (the wheel is about 450 MB)"


@dataclass(frozen=True)
class ReferenceConfig:
	voxel_length: float = 0.01
	sdf_trunc: float = 0.04
	max_depth: float = 4.0


@dataclass(frozen=True, eq=False)
class DatasetInputs:
	root: Path
	width: int
	height: int
	intrinsics: np.ndarray  # 3x3
	depth_scale: float  # raw depth units per metre
	rgb_files: tuple
	depth_files: tuple
	poses: np.ndarray  # (P, 4, 4) T_world_camera; row i belongs to frame i of the sorted rgb files


def log(message: str) -> None:
	print(f"[reference-mesh] {message}", file=sys.stderr, flush=True)


class _EmptyPackages(importlib.abc.MetaPathFinder, importlib.abc.Loader):
	"""Stand-ins for the packages that `import open3d` loads although the reconstruction never uses them: plotly and dash
	(its web visualizer) and addict (its ML toolkit). They are dependencies of the wheel, but `pip install --no-deps open3d`
	(which saves downloading them) leaves them out. This finder is consulted last, so a real installation always wins."""

	PACKAGES = ("addict", "dash", "plotly")

	def find_spec(self, name, path, target=None):
		if name.split(".")[0] not in self.PACKAGES:
			return None
		return importlib.machinery.ModuleSpec(name, self, is_package=True)

	def create_module(self, spec):
		module = types.ModuleType(spec.name)
		module.__path__ = []
		module.__getattr__ = lambda attribute: type(attribute, (dict,), {})  # a class: `class Config(addict.Dict)` must work
		return module

	def exec_module(self, module):
		pass


def _import_open3d():
	finder = _EmptyPackages()
	sys.meta_path.append(finder)
	try:
		import open3d
	finally:
		sys.meta_path.remove(finder)
	return open3d


def _open3d_version():
	try:
		return getattr(_import_open3d(), "__version__", None)
	except ImportError:
		return None


# ---------------------------------------------------------------- fingerprint and cache contract

def file_sha256(path: Path) -> str:
	digest = hashlib.sha256()
	with open(path, "rb") as f:
		for chunk in iter(lambda: f.read(1 << 20), b""):
			digest.update(chunk)
	return digest.hexdigest()


def dataset_inventory(dataset: Path) -> dict:
	"""What the reconstruction depends on, without reading the images: content hashes of the three small metadata files
	and the sorted relative path, size and nanosecond modification time of every rgb and depth file."""
	dataset = Path(dataset)
	if not (dataset / "rgb").is_dir():
		raise ValueError(f"{dataset}: not a prepared dataset (no rgb/ folder)")

	def listing(folder: str, suffixes: tuple) -> list:
		directory = dataset / folder
		if not directory.is_dir():
			return []
		rows = []
		for path in sorted(p for p in directory.iterdir() if p.suffix.lower() in suffixes):
			status = path.stat()
			rows.append([f"{folder}/{path.name}", status.st_size, status.st_mtime_ns])
		return rows

	meta = {}
	for name in ("camera_info.json", "pose/poses.txt", "timestamps.txt"):
		file = dataset / name
		meta[name] = file_sha256(file) if file.is_file() else None
	return {"meta": meta, "rgb": listing("rgb", RGB_SUFFIXES), "depth": listing("depth", DEPTH_SUFFIXES)}


def dataset_fingerprint(inventory: dict, config: ReferenceConfig) -> str:
	payload = {"schema": CACHE_SCHEMA, "pose_convention": POSE_CONVENTION, "inventory": inventory, "config": asdict(config)}
	return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def resolve_cache_paths(dataset: Path, override=None) -> tuple:
	root = Path(override) if override else Path(dataset) / ".daaam" / "reference_mesh"
	return root / f"{CACHE_STEM}.npz", root / f"{CACHE_STEM}.json"


def validate_arrays(arrays) -> None:
	"""ValueError unless the arrays are a finite, non-empty indexed triangle mesh in the layout of the cache."""
	for name, (dtype, width) in ARRAYS.items():
		if name not in arrays:
			raise ValueError(f"missing array '{name}'")
		array = arrays[name]
		if array.ndim != 2 or array.shape[1] != width:
			raise ValueError(f"'{name}' must have shape (N, {width}), got {tuple(array.shape)}")
		if array.dtype != np.dtype(dtype):
			raise ValueError(f"'{name}' must be {dtype}, got {array.dtype}")
	count = len(arrays["positions"])
	if count == 0 or len(arrays["triangles"]) == 0:
		raise ValueError("the mesh is empty")
	if len(arrays["colors"]) != count or len(arrays["normals"]) != count:
		raise ValueError("positions, colors and normals must have the same length")
	if not np.isfinite(arrays["positions"]).all() or not np.isfinite(arrays["normals"]).all():
		raise ValueError("positions and normals must be finite")
	if int(arrays["triangles"].max()) >= count:
		raise ValueError("a triangle refers to a vertex that does not exist")


def _npz_members(path: Path) -> dict:
	"""name -> (shape, dtype) of the arrays of an .npz, read from the .npy headers only."""
	members = {}
	with zipfile.ZipFile(path) as archive:
		for info in archive.infolist():
			if not info.filename.endswith(".npy"):
				continue
			with archive.open(info) as f:
				version = np.lib.format.read_magic(f)
				reader = np.lib.format.read_array_header_1_0 if version == (1, 0) else np.lib.format.read_array_header_2_0
				shape, _, dtype = reader(f)
			members[info.filename[: -len(".npy")]] = (tuple(shape), np.dtype(dtype))
	return members


def cache_problem(dataset: Path, config: ReferenceConfig, override=None, inventory=None):
	"""Why the cache of this dataset cannot be used (a short sentence), or None when it is valid."""
	npz, manifest_path = resolve_cache_paths(dataset, override)
	if not manifest_path.is_file() or not npz.is_file():
		return "no cache yet"
	try:
		manifest = json.loads(manifest_path.read_text())
		schema = manifest["schema"]
		parameters = manifest["parameters"]
		fingerprint = manifest["fingerprint"]
		expected_bytes, expected_sha = manifest["files"]["npz_bytes"], manifest["files"]["npz_sha256"]
		mesh = manifest["mesh"]
	except (OSError, ValueError, KeyError, TypeError):
		return "the manifest is unreadable"
	if schema != CACHE_SCHEMA:
		return f"the cache has schema {schema}, this builder writes {CACHE_SCHEMA}"
	if parameters != _parameters(config):
		return "the cache was built with other reconstruction parameters"
	inventory = inventory if inventory is not None else dataset_inventory(dataset)
	if fingerprint != dataset_fingerprint(inventory, config):
		other = manifest.get("dataset")
		same = other == str(Path(dataset).resolve())
		return "the dataset changed since the cache was built" if same else f"the cache belongs to another dataset ({other})"
	try:
		if npz.stat().st_size != expected_bytes or file_sha256(npz) != expected_sha:
			return "the arrays file does not match its manifest"
		members = _npz_members(npz)
	except (OSError, ValueError, zipfile.BadZipFile):
		return "the arrays file is unreadable"
	for name, (dtype, width) in ARRAYS.items():
		rows = mesh["triangles"] if name == "triangles" else mesh["vertices"]
		if members.get(name) != ((rows, width), np.dtype(dtype)):
			return f"array '{name}' does not match the manifest"
	return None


def load_valid_cache(dataset: Path, config: ReferenceConfig, override=None):
	"""Path of the valid .npz cache of the dataset, or None."""
	return None if cache_problem(dataset, config, override) else resolve_cache_paths(dataset, override)[0]


def _parameters(config: ReferenceConfig) -> dict:
	return {**asdict(config), "pose_convention": POSE_CONVENTION, "color": "RGB8"}


def prepare_cache_dir(directory: Path) -> None:
	try:
		directory.mkdir(parents=True, exist_ok=True)
	except OSError as error:
		raise OSError(f"cannot create {directory}: {error}; set REFERENCE_MESH_CACHE to a writable directory") from error


def publish_cache(dataset: Path, config: ReferenceConfig, arrays: dict, stats: dict, cache_dir=None, inventory=None, seconds=None) -> Path:
	"""Write the arrays and the manifest atomically (temporary siblings, then rename) and return the .npz path."""
	validate_arrays(arrays)
	dataset = Path(dataset)
	inventory = inventory if inventory is not None else dataset_inventory(dataset)
	npz, manifest_path = resolve_cache_paths(dataset, cache_dir)
	prepare_cache_dir(npz.parent)
	tag = f".{os.getpid()}.tmp"
	tmp_npz, tmp_manifest = npz.with_name(f".{npz.name}{tag}"), manifest_path.with_name(f".{manifest_path.name}{tag}")
	try:
		with open(tmp_npz, "wb") as f:
			np.savez_compressed(f, **arrays)
		positions = arrays["positions"]
		manifest = {
			"schema": CACHE_SCHEMA,
			"created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
			"dataset": str(dataset.resolve()),
			"fingerprint": dataset_fingerprint(inventory, config),
			"parameters": _parameters(config),
			"frames": {"rgb": len(inventory["rgb"]), "depth": len(inventory["depth"]), "integrated": int(stats["integrated"]), "skipped": int(stats["skipped"])},
			"mesh": {"vertices": len(positions), "triangles": len(arrays["triangles"]), "bounds_min": positions.min(axis=0).tolist(), "bounds_max": positions.max(axis=0).tolist()},
			"files": {"npz": npz.name, "npz_bytes": tmp_npz.stat().st_size, "npz_sha256": file_sha256(tmp_npz)},
			"versions": {"python": platform.python_version(), "numpy": np.__version__, "open3d": _open3d_version()},
			"inventory": inventory,
		}
		if seconds is not None:
			manifest["build_seconds"] = round(seconds, 1)
		tmp_manifest.write_text(json.dumps(manifest, indent=1, sort_keys=True))
		os.replace(tmp_npz, npz)
		os.replace(tmp_manifest, manifest_path)
	finally:
		for tmp in (tmp_npz, tmp_manifest):
			tmp.unlink(missing_ok=True)
	return npz


# ---------------------------------------------------------------- reconstruction

def _parse_poses(path: Path) -> np.ndarray:
	rows = []
	for number, line in enumerate(path.read_text().splitlines(), 1):
		if not line.strip():
			continue
		values = line.split()
		if len(values) != 16:
			raise ValueError(f"{path}: line {number} has {len(values)} numbers, expected 16 (a row-major 4x4 {POSE_CONVENTION})")
		rows.append([float(v) for v in values])
	return np.array(rows, np.float64).reshape(-1, 4, 4)


def read_prepared_dataset(dataset: Path) -> DatasetInputs:
	"""Camera, file lists and poses of a prepared dataset; ValueError for anything that makes the reconstruction meaningless."""
	dataset = Path(dataset)
	if not (dataset / "rgb").is_dir():
		raise ValueError(f"{dataset}: not a prepared dataset (no rgb/ folder)")
	info_file = dataset / "camera_info.json"
	try:
		info = json.loads(info_file.read_text())
		width, height = int(info["width"]), int(info["height"])
		intrinsics = np.array(info["intrinsics"], np.float64).reshape(3, 3)
		depth_scale = float(info["depth_scale"])
	except (OSError, ValueError, KeyError, TypeError) as error:
		raise ValueError(f"{info_file}: {error!r}; it needs width, height, a 3x3 intrinsics matrix and depth_scale") from error
	if width <= 0 or height <= 0 or not np.isfinite(intrinsics).all() or intrinsics[0, 0] <= 0 or intrinsics[1, 1] <= 0 or not depth_scale > 0:
		raise ValueError(f"{info_file}: invalid calibration (size {width}x{height}, fx {intrinsics[0, 0]}, fy {intrinsics[1, 1]}, depth_scale {depth_scale})")
	poses_file = dataset / "pose" / "poses.txt"
	if not poses_file.is_file():
		raise ValueError(f"{poses_file}: not found")
	rgb = tuple(sorted(p for p in (dataset / "rgb").iterdir() if p.suffix.lower() in RGB_SUFFIXES))
	depth_dir = dataset / "depth"
	depth = tuple(sorted(p for p in depth_dir.iterdir() if p.suffix.lower() in DEPTH_SUFFIXES)) if depth_dir.is_dir() else ()
	return DatasetInputs(dataset, width, height, intrinsics, depth_scale, rgb, depth, _parse_poses(poses_file))


def integrate_reference_mesh(inputs: DatasetInputs, config: ReferenceConfig, stats=None) -> dict:
	"""Integrate every frame once and return the cleaned mesh as the arrays of the cache. `stats` receives the counts."""
	import cv2

	o3d = _import_open3d()
	stats = {} if stats is None else stats
	total = len(inputs.rgb_files)
	if len(inputs.depth_files) != total or len(inputs.poses) != total:
		log(f"WARNING: {total} rgb images, {len(inputs.depth_files)} depth images and {len(inputs.poses)} poses; frames are paired by position, missing ones are skipped")
	K = inputs.intrinsics
	intrinsic = o3d.camera.PinholeCameraIntrinsic(inputs.width, inputs.height, K[0, 0], K[1, 1], K[0, 2], K[1, 2])
	volume = o3d.pipelines.integration.ScalableTSDFVolume(
		voxel_length=config.voxel_length,
		sdf_trunc=config.sdf_trunc,
		color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8,
	)
	skipped, notes, integrated = 0, [], 0
	started = time.monotonic()
	for i, rgb_path in enumerate(inputs.rgb_files):
		name = rgb_path.stem
		pose = inputs.poses[i] if i < len(inputs.poses) else None
		depth_path = inputs.depth_files[i] if i < len(inputs.depth_files) else None
		reason = None
		if pose is None or not np.isfinite(pose).all() or not np.allclose(pose[3], [0.0, 0.0, 0.0, 1.0], atol=1e-6):
			reason = "no finite pose"
		elif depth_path is None:
			reason = "no depth image"
		else:
			colour = cv2.imread(str(rgb_path), cv2.IMREAD_COLOR)
			depth = cv2.imread(str(depth_path), cv2.IMREAD_UNCHANGED)
			if colour is None or depth is None or depth.ndim != 2 or depth.dtype not in (np.uint16, np.float32):
				reason = "rgb or depth image cannot be decoded (depth must be a 16-bit PNG)"
			else:
				for label, image in (("rgb", colour), ("depth", depth)):
					if image.shape[:2] != (inputs.height, inputs.width):
						raise ValueError(f"frame {name}: {label} image is {image.shape[1]}x{image.shape[0]} but camera_info.json says {inputs.width}x{inputs.height}")
		if reason is not None:
			skipped += 1
			if len(notes) < 10:
				notes.append(f"frame {name}: {reason}")
			continue
		colour = np.ascontiguousarray(cv2.cvtColor(colour, cv2.COLOR_BGR2RGB))
		rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
			o3d.geometry.Image(colour),
			o3d.geometry.Image(np.ascontiguousarray(depth)),
			depth_scale=inputs.depth_scale,
			depth_trunc=config.max_depth,
			convert_rgb_to_intensity=False,
		)
		volume.integrate(rgbd, intrinsic, np.linalg.inv(pose))
		integrated += 1
		if integrated % 100 == 0:
			log(f"integrated {integrated} of {total} frames ({time.monotonic() - started:.0f} s)")
	stats["integrated"], stats["skipped"] = integrated, skipped
	for note in notes:
		log(f"skipped {note}")
	if skipped:
		ratio = skipped / max(total, 1)
		log(f"{'WARNING: ' if ratio > SKIPPED_WARNING_RATIO else ''}skipped {skipped} of {total} frames ({ratio * 100:.1f} %)")
	if not integrated:
		raise ValueError("no frame could be integrated")
	log(f"integrated {integrated} of {total} frames in {time.monotonic() - started:.0f} s; extracting the mesh")

	mesh = volume.extract_triangle_mesh()
	mesh.remove_duplicated_vertices()
	mesh.remove_duplicated_triangles()
	mesh.remove_degenerate_triangles()
	mesh.remove_unreferenced_vertices()
	mesh.compute_vertex_normals()
	positions = np.asarray(mesh.vertices, np.float64)
	if len(positions) == 0 or len(np.asarray(mesh.triangles)) == 0:
		raise ValueError("the reconstructed mesh is empty (no surface within the depth range of any frame)")
	colours = np.asarray(mesh.vertex_colors, np.float64)
	if len(colours) != len(positions):
		colours = np.full((len(positions), 3), 0.5)
	arrays = {
		"positions": positions.astype(np.float32),
		"colors": np.clip(np.round(colours * 255.0), 0, 255).astype(np.uint8),
		"normals": np.asarray(mesh.vertex_normals, np.float64).astype(np.float32),
		"triangles": np.asarray(mesh.triangles).astype(np.uint32),
	}
	validate_arrays(arrays)
	return arrays


def build_or_reuse(dataset: Path, config=None, cache_dir=None, force: bool = False) -> Path:
	"""The path of the valid cache of this dataset: reused when it is valid (no Open3D needed), else built and published."""
	dataset, config = Path(dataset), config or ReferenceConfig()
	inventory = dataset_inventory(dataset)
	npz, _ = resolve_cache_paths(dataset, cache_dir)
	if not force:
		problem = cache_problem(dataset, config, cache_dir, inventory)
		if problem is None:
			log(f"reusing the valid 1 cm reference mesh {npz}")
			return npz
		log(f"building the 1 cm reference mesh of {dataset} ({problem})")
	else:
		log(f"rebuilding the 1 cm reference mesh of {dataset} (--force)")
	prepare_cache_dir(npz.parent)  # fail before the reconstruction, not after
	stats = {}
	started = time.monotonic()
	arrays = integrate_reference_mesh(read_prepared_dataset(dataset), config, stats)
	published = publish_cache(dataset, config, arrays, stats, cache_dir, inventory, time.monotonic() - started)
	log(f"{len(arrays['positions']):,} vertices, {len(arrays['triangles']):,} triangles, {published.stat().st_size / 1e6:.1f} MB -> {published}{_peak_memory_note()}")
	return published


def _peak_memory_note() -> str:
	try:
		import resource

		return f" (peak memory {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB)"  # ru_maxrss is in KiB on Linux
	except (ImportError, OSError, ValueError):
		return ""


@contextlib.contextmanager
def stdout_to_stderr():
	"""Native libraries (Open3D writes its log lines to file descriptor 1) must not reach stdout, which carries only the cache path."""
	sys.stdout.flush()
	saved = os.dup(1)
	try:
		os.dup2(2, 1)
		yield
	finally:
		sys.stdout.flush()
		os.dup2(saved, 1)
		os.close(saved)


def main(argv=None) -> int:
	parser = argparse.ArgumentParser(description="Build and cache the 1 cm Open3D TSDF reference mesh of a prepared RGB-D dataset")
	parser.add_argument("dataset", type=Path, help="prepared dataset folder (rgb/, depth/, pose/poses.txt, camera_info.json)")
	parser.add_argument("--cache-dir", type=Path, default=os.environ.get("REFERENCE_MESH_CACHE") or None,
	                    help="directory for the cache files instead of <dataset>/.daaam/reference_mesh (default: $REFERENCE_MESH_CACHE); one directory per dataset")
	parser.add_argument("--force", action="store_true", help="rebuild even when a valid cache exists")
	args = parser.parse_args(argv)
	try:
		with stdout_to_stderr():
			path = build_or_reuse(args.dataset, ReferenceConfig(), args.cache_dir, args.force)
	except ImportError as error:
		log(f"Open3D is not available ({error}); {INSTALL_HINT}")
		return 1
	except (ValueError, OSError) as error:
		log(f"failed: {error}")
		return 1
	print(path)
	return 0


if __name__ == "__main__":
	sys.exit(main())
