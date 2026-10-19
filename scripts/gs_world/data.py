"""Posed RGB-D folders in the layout of DAAAM's ImageSequenceDataset (as written by scripts/prepare_tum_rgbd.py).

	<root>/
		rgb/000000.png ...       8-bit colour images, sorted by name
		depth/000000.png ...     16-bit depth in camera_info.json depth_scale units per metre (0 = no measurement)
		pose/poses.txt           one row-major 4x4 T_world_camera (camera-to-world, optical axes) per line
		timestamps.txt           optional: one time in seconds per frame
		camera_info.json         width, height, intrinsics (3x3), distortion (all zero: pinhole), depth_scale, fps
		valid_mask.png           optional: 255 where an undistorted image has real pixels, 0 on its black border

Every gs_world writer uses 5000 depth units per metre, the scale scripts/run_tum_example.sh passes to the pipeline.
"""

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from .geometry import is_rigid, pose_matrix

DEPTH_SCALE = 5000.0
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp")
DATASETS_ROOT = Path(os.environ.get("GS_DATASETS_ROOT", "/datasets"))


@dataclass(frozen=True)
class Camera:
	"""Pinhole camera; K uses the OpenCV/ROS convention (the centre of pixel (0, 0) is at (0, 0))."""

	width: int
	height: int
	K: np.ndarray

	@classmethod
	def from_info(cls, info: Dict) -> "Camera":
		K = np.asarray(info["intrinsics"], dtype=np.float64)
		if K.shape != (3, 3) or K[0, 0] <= 0 or K[1, 1] <= 0:
			raise ValueError(f"camera_info intrinsics must be a 3x3 matrix with positive focal lengths, got {K.tolist()}")
		return cls(int(info["width"]), int(info["height"]), K)

	def scaled(self, factor: float) -> "Camera":
		"""The camera of the image resized to 1/factor of its size."""
		if factor == 1:
			return self
		width = max(1, int(round(self.width / factor)))
		height = max(1, int(round(self.height / factor)))
		sx, sy = width / self.width, height / self.height
		K = self.K.copy()
		K[0, 0] *= sx
		K[1, 1] *= sy
		K[0, 2] = (K[0, 2] + 0.5) * sx - 0.5
		K[1, 2] = (K[1, 2] + 0.5) * sy - 0.5
		return Camera(width, height, K)

	def gsplat_K(self) -> np.ndarray:
		"""K for gsplat, which evaluates pixel (row i, column j) at (j + 0.5, i + 0.5)."""
		K = self.K.copy()
		K[0, 2] += 0.5
		K[1, 2] += 0.5
		return K

	def info(self) -> Dict:
		return {"width": int(self.width), "height": int(self.height), "intrinsics": self.K.tolist()}


def resolve_dataset(name: str) -> Path:
	"""A dataset directory from a path or a short name: tum_fr1_room or fr1_room both mean /datasets/tum_fr1_room."""
	path = Path(name)
	candidates = [path] if path.is_absolute() else [path, DATASETS_ROOT / name, DATASETS_ROOT / f"tum_{name}"]
	for candidate in candidates:
		if (candidate / "rgb").is_dir():
			return candidate
	available = sorted(p.name for p in DATASETS_ROOT.iterdir() if (p / "rgb").is_dir()) if DATASETS_ROOT.is_dir() else []
	raise FileNotFoundError(f"no RGB-D dataset at {name!r} (needs an rgb/ folder); datasets in {DATASETS_ROOT}: "
		f"{', '.join(available) or 'none'}")


def read_poses(path: Path) -> np.ndarray:
	"""poses.txt: one row-major 4x4 matrix (16 numbers) per non-empty line."""
	rows = []
	with open(path, "r", encoding="utf-8") as f:
		for number, line in enumerate(f, 1):
			line = line.strip()
			if not line or line.startswith("#"):
				continue
			values = [float(v) for v in line.split()]
			if len(values) != 16:
				raise ValueError(f"{path}:{number}: expected 16 numbers (row-major 4x4 T_world_camera), got {len(values)}")
			rows.append(np.asarray(values).reshape(4, 4))
	return np.asarray(rows, dtype=np.float64).reshape(-1, 4, 4)


def write_poses(path: Path, poses: np.ndarray) -> None:
	with open(path, "w", encoding="utf-8") as f:
		for T in np.asarray(poses, dtype=np.float64):
			f.write(" ".join(f"{v:.9g}" for v in T.reshape(-1)) + "\n")


def read_trajectory(path: Path) -> Tuple[np.ndarray, Optional[np.ndarray]]:
	"""A camera trajectory as (T_world_camera [N, 4, 4], times in seconds or None).

	Accepted line formats (whitespace separated, '#' comments): 16 numbers (row-major 4x4, like pose/poses.txt),
	8 numbers (TUM: time tx ty tz qx qy qz qw) or 7 numbers (tx ty tz qx qy qz qw).
	"""
	poses, times = [], []
	width = None
	with open(path, "r", encoding="utf-8") as f:
		for number, line in enumerate(f, 1):
			line = line.strip()
			if not line or line.startswith("#"):
				continue
			values = [float(v) for v in line.replace(",", " ").split()]
			if width is None:
				width = len(values)
				if width not in (7, 8, 16):
					raise ValueError(f"{path}:{number}: expected 16, 8 (TUM) or 7 numbers per line, got {width}")
			if len(values) != width:
				raise ValueError(f"{path}:{number}: {len(values)} numbers, earlier lines have {width}")
			if width == 16:
				poses.append(np.asarray(values).reshape(4, 4))
			else:
				t, rest = (values[0], values[1:]) if width == 8 else (None, values)
				poses.append(pose_matrix(rest[:3], rest[3:7]))
				if t is not None:
					times.append(t)
	poses = np.asarray(poses, dtype=np.float64).reshape(-1, 4, 4)
	return poses, (np.asarray(times) if times else None)


def depth_to_png(depth_m: np.ndarray, depth_scale: float = DEPTH_SCALE) -> np.ndarray:
	"""Metric depth to 16-bit units; missing, non-finite and unrepresentable values become 0."""
	d = np.asarray(depth_m, dtype=np.float64) * depth_scale
	valid = np.isfinite(d) & (d > 0) & (d < 65535.5)
	return np.where(valid, np.round(d), 0).astype(np.uint16)


def filter_depth_edges(depth_m: np.ndarray, max_relative_jump: float = 0.1) -> np.ndarray:
	"""Drop depth pixels next to a depth discontinuity (RGB-D 'flying pixels').

	A pixel is dropped when the measured depths of its 3x3 neighbourhood differ by more than max_relative_jump times
	its own depth; missing measurements do not count as a discontinuity.
	"""
	d = np.asarray(depth_m, dtype=np.float32).copy()
	valid = d > 0
	kernel = np.ones((3, 3), np.uint8)
	d_max = cv2.dilate(d, kernel)
	d_min = cv2.erode(np.where(valid, d, np.float32(1e6)), kernel)
	d[valid & ((d_max - d_min) > max_relative_jump * d)] = 0.0
	return d


def _resize(image: np.ndarray, width: int, height: int, interpolation: int) -> np.ndarray:
	if image.shape[1] == width and image.shape[0] == height:
		return image
	return cv2.resize(image, (width, height), interpolation=interpolation)


class RGBDSequence:
	"""Read-only access to one posed RGB-D folder."""

	def __init__(self, root: Path):
		self.root = Path(root)
		info_path = self.root / "camera_info.json"
		if not info_path.is_file():
			raise FileNotFoundError(f"{info_path} is missing")
		self.info = json.loads(info_path.read_text(encoding="utf-8"))
		self.camera = Camera.from_info(self.info)
		distortion = [float(v) for v in (self.info.get("distortion") or [])]
		if any(abs(v) > 1e-12 for v in distortion):
			raise ValueError(f"{info_path}: distortion {distortion} is not zero; undistort the images first "
				"(scripts/prepare_tum_rgbd.py and gs_world.rosbag_to_rgbd do that)")
		self.depth_scale = float(self.info.get("depth_scale", DEPTH_SCALE))
		self.fps = float(self.info.get("fps") or 0.0)
		self.source = str(self.info.get("source", ""))

		self.rgb_files = sorted(p for p in (self.root / "rgb").iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
		depth_dir = self.root / "depth"
		self.depth_files = sorted(p for p in depth_dir.iterdir() if p.suffix.lower() == ".png") if depth_dir.is_dir() else []
		self.poses = read_poses(self.root / "pose" / "poses.txt")
		n = len(self.rgb_files)
		if n == 0:
			raise ValueError(f"no images in {self.root / 'rgb'}")
		if len(self.poses) != n:
			raise ValueError(f"{self.root}: {n} images but {len(self.poses)} poses")
		if self.depth_files and len(self.depth_files) != n:
			raise ValueError(f"{self.root}: {n} images but {len(self.depth_files)} depth images")
		bad = [i for i, T in enumerate(self.poses) if not is_rigid(T, tol=1e-3)]
		if bad:
			raise ValueError(f"{self.root}: pose {bad[0]} (and {len(bad) - 1} more) is not a rigid transform")
		self.timestamps = self._read_timestamps(n)

	def _read_timestamps(self, n: int) -> np.ndarray:
		path = self.root / "timestamps.txt"
		if path.is_file():
			values = [float(v) for v in path.read_text(encoding="utf-8").split()]
			if len(values) == n:
				return np.asarray(values)
		return np.arange(n) / (self.fps or 30.0)

	def __len__(self) -> int:
		return len(self.rgb_files)

	@property
	def has_depth(self) -> bool:
		return bool(self.depth_files)

	def rgb(self, i: int, scale: float = 1.0) -> np.ndarray:
		"""Image i as uint8 RGB, resized to 1/scale."""
		image = cv2.imread(str(self.rgb_files[i]), cv2.IMREAD_COLOR)
		if image is None:
			raise IOError(f"cannot read {self.rgb_files[i]}")
		if image.shape[:2] != (self.camera.height, self.camera.width):
			raise ValueError(f"{self.rgb_files[i]} is {image.shape[1]}x{image.shape[0]}, camera_info.json says "
				f"{self.camera.width}x{self.camera.height}")
		cam = self.camera.scaled(scale)
		image = _resize(image, cam.width, cam.height, cv2.INTER_AREA)
		return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

	def depth(self, i: int, scale: float = 1.0) -> np.ndarray:
		"""Depth i in metres (float32, 0 = no measurement), resized to 1/scale without blending depths."""
		raw = cv2.imread(str(self.depth_files[i]), cv2.IMREAD_UNCHANGED)
		if raw is None:
			raise IOError(f"cannot read {self.depth_files[i]}")
		depth = raw.astype(np.float32) / np.float32(self.depth_scale)
		cam = self.camera.scaled(scale)
		return _resize(depth, cam.width, cam.height, cv2.INTER_NEAREST)

	def valid_mask(self, scale: float = 1.0, samples: int = 30) -> np.ndarray:
		"""Pixels that hold image content (bool HxW at 1/scale).

		Uses valid_mask.png when the converter wrote one. Otherwise the black border that undistortion leaves (for
		example in TUM freiburg1 images) is found as pixels that are pure black in at least 90 % of a sample of frames
		and connected to the image border; the remaining region is shrunk by one pixel to drop blended border pixels.
		"""
		cam = self.camera.scaled(scale)
		path = self.root / "valid_mask.png"
		if path.is_file():
			mask = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
			return _resize(mask, cam.width, cam.height, cv2.INTER_NEAREST) > 127
		ids = np.unique(np.linspace(0, len(self) - 1, min(samples, len(self))).astype(int))
		black = np.zeros((self.camera.height, self.camera.width), np.int32)
		for i in ids:
			image = cv2.imread(str(self.rgb_files[i]), cv2.IMREAD_COLOR)
			black += np.all(image == 0, axis=2)
		candidate = (black >= 0.9 * len(ids)).astype(np.uint8)
		valid = np.ones_like(candidate, dtype=bool)
		if candidate.any():
			count, labels = cv2.connectedComponents(candidate, connectivity=4)
			border = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
			invalid = np.isin(labels, [label for label in border if label != 0])
			if invalid.any():
				valid = cv2.erode((~invalid).astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
		return _resize(valid.astype(np.uint8), cam.width, cam.height, cv2.INTER_NEAREST).astype(bool)


class LayoutWriter:
	"""Write a posed RGB-D sequence frame by frame.

	Frames go into '<out>.partial'; close() writes the metadata and renames it to <out>, so an interrupted run never
	leaves a folder that looks complete. An existing <out> is only replaced with overwrite=True.
	"""

	def __init__(self, out: Path, camera: Camera, fps: float, source: str, depth_scale: float = DEPTH_SCALE,
			overwrite: bool = False, extra_info: Optional[Dict] = None):
		self.out = Path(out)
		if self.out.exists() and not overwrite:
			raise FileExistsError(f"{self.out} already exists; choose another output or pass --overwrite")
		self.partial = self.out.with_name(self.out.name + ".partial")
		if self.partial.exists():
			shutil.rmtree(self.partial)
		for sub in ("rgb", "depth", "pose"):
			(self.partial / sub).mkdir(parents=True)
		self.camera = camera
		self.fps = float(fps)
		self.source = source
		self.depth_scale = float(depth_scale)
		self.extra_info = dict(extra_info or {})
		self.poses: List[np.ndarray] = []
		self.stamps: List[float] = []

	def add(self, rgb: np.ndarray, depth_m: Optional[np.ndarray], T_world_camera: np.ndarray, timestamp: float) -> int:
		"""Append one frame (RGB uint8 HxWx3, metric depth HxW or None); returns its index."""
		if rgb.shape[:2] != (self.camera.height, self.camera.width):
			raise ValueError(f"frame is {rgb.shape[1]}x{rgb.shape[0]}, the camera is {self.camera.width}x{self.camera.height}")
		index = len(self.poses)
		name = f"{index:06d}.png"
		if not cv2.imwrite(str(self.partial / "rgb" / name), cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2BGR)):
			raise IOError(f"cannot write {self.partial / 'rgb' / name}")
		if depth_m is not None:
			if depth_m.shape != rgb.shape[:2]:
				raise ValueError("depth and RGB sizes differ")
			if not cv2.imwrite(str(self.partial / "depth" / name), depth_to_png(depth_m, self.depth_scale)):
				raise IOError(f"cannot write {self.partial / 'depth' / name}")
		self.poses.append(np.asarray(T_world_camera, dtype=np.float64))
		self.stamps.append(float(timestamp))
		return index

	def close(self, valid_mask: Optional[np.ndarray] = None) -> Path:
		if not self.poses:
			shutil.rmtree(self.partial)
			raise ValueError("no frame was written")
		write_poses(self.partial / "pose" / "poses.txt", np.asarray(self.poses))
		with open(self.partial / "timestamps.txt", "w", encoding="utf-8") as f:
			f.write("\n".join(f"{t:.6f}" for t in self.stamps) + "\n")
		if not any((self.partial / "depth").iterdir()):
			(self.partial / "depth").rmdir()
		if valid_mask is not None:
			if not cv2.imwrite(str(self.partial / "valid_mask.png"), np.where(valid_mask, 255, 0).astype(np.uint8)):
				raise IOError(f"cannot write {self.partial / 'valid_mask.png'}")
		info = {
			**self.camera.info(),
			"distortion": [0.0] * 5,
			"depth_scale": self.depth_scale,
			"fps": self.fps,
			"source": self.source,
			**self.extra_info,
		}
		(self.partial / "camera_info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n",
			encoding="utf-8")
		if self.out.exists():
			shutil.rmtree(self.out)
		self.partial.rename(self.out)
		return self.out
