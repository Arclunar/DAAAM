"""Dataset layouts behind a replay bundle: which image belongs to a snapshot or to a DAM description source.

The finalizer (scripts/replay_bundle.py) never guesses the dataset from a folder name: the caller names the kind
(`tum` or `coda`) and this module maps run time and run frame IDs onto that dataset's files.

* TUM (`scripts/prepare_tum_rgbd.py` layout): `rgb/*.png|jpg` with zero-padded names, `depth/<same stem>.png`, the depth
  unit in `camera_info.json` (`depth_scale`, default 1000 per metre). A run starts at the first frame and stamps frame
  k with k / fps; pipeline frame IDs are one-based.
* CODa: `2d_rect/<camera>/<seq>/2d_rect_<camera>_<seq>_<n>.png`, `3d_raw_estimated/<camera>/<seq>/depth_<camera>_<seq>_<n>.png`
  (FoundationStereo, millimetres) and `timestamps/<seq>.txt`, one stamp per frame. The file numbers are not zero-padded,
  so frames are ordered by their number, never by name. A run covers frames start_frame .. start_frame+frame_count-1;
  its one-based frame ID f is dataset frame start_frame + f - 1 unless the recorded image stamp says otherwise (a node
  that dropped frames counts only the frames it processed): a description source whose stamp matches no frame of the
  run has no image.

Standard library only.
"""

import bisect
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

try:   # Python 3.8 has typing.Protocol; keep it optional for type checkers only
	from typing import Protocol
except ImportError:   # pragma: no cover
	Protocol = object   # type: ignore

STAMP_TOLERANCE = 5e-4   # seconds; CODa frames are 100 ms apart and stamps are stored with microseconds
_TRAILING_NUMBER = re.compile(r"_(\d+)$")


class DatasetAssets(Protocol):
	"""What the finalizer needs from a dataset; created only through dataset_assets(kind, ...)."""

	kind: str
	camera: str
	depth_scale: float
	default_max_depth: float
	frame_total: int

	def run_range(self) -> Tuple[int, int]: ...
	def rgb_path(self, index: int) -> Optional[Path]: ...
	def depth_path(self, index: int) -> Optional[Path]: ...
	def timeline_index(self, time_since_origin: float) -> Optional[int]: ...
	def description_index(self, source: Mapping[str, Any]) -> Optional[int]: ...
	def dataset_time_origin(self) -> Optional[float]: ...


def _frame_id(source: Mapping[str, Any]) -> Optional[int]:
	try:
		frame_id = int(source["frame_id"])
	except (KeyError, TypeError, ValueError):
		return None
	return frame_id if frame_id >= 1 else None


def _stamp(source: Mapping[str, Any]) -> Optional[float]:
	try:
		return float(source["timestamp"]) if source.get("timestamp") is not None else None
	except (TypeError, ValueError):
		return None


class _RunFrames:
	"""Common run-range logic over `frame_total` dataset frames."""

	def __init__(self, fps: float, start_frame: int, frame_count: Optional[int]):
		if fps <= 0:
			raise ValueError("fps must be positive")
		if start_frame < 0 or (frame_count is not None and frame_count < 1):
			raise ValueError("start_frame must be >= 0 and frame_count positive")
		self.fps = float(fps)
		self.start_frame = int(start_frame)
		self.frame_count = frame_count

	def run_range(self) -> Tuple[int, int]:
		end = self.frame_total if self.frame_count is None else min(self.frame_total, self.start_frame + self.frame_count)
		return min(self.start_frame, end), end

	def timeline_index(self, time_since_origin: float) -> Optional[int]:
		first, end = self.run_range()
		if end <= first:
			return None
		return int(min(max(round(time_since_origin * self.fps), first), end - 1))

	def in_run(self, index: int) -> bool:
		first, end = self.run_range()
		return first <= index < end


class TumDatasetAssets(_RunFrames):
	kind = "tum"
	camera = "rgb"
	default_max_depth = 4.0

	def __init__(self, root: Path, fps: float, start_frame: int = 0, frame_count: Optional[int] = None):
		super().__init__(fps, start_frame, frame_count)
		self.root = Path(root)
		self.files = sorted((self.root / "rgb").glob("*.png")) + sorted((self.root / "rgb").glob("*.jpg"))
		self.frame_total = len(self.files)
		info_path = self.root / "camera_info.json"
		info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.is_file() else {}
		self.depth_scale = float(info.get("depth_scale", 1000.0))

	def rgb_path(self, index: int) -> Optional[Path]:
		return self.files[index] if 0 <= index < self.frame_total else None

	def depth_path(self, index: int) -> Optional[Path]:
		rgb = self.rgb_path(index)
		depth = self.root / "depth" / f"{rgb.stem}.png" if rgb is not None else None
		return depth if depth is not None and depth.is_file() else None

	def description_index(self, source: Mapping[str, Any]) -> Optional[int]:
		frame_id = _frame_id(source)
		if frame_id is None:
			return None
		index = self.start_frame + frame_id - 1   # one-based pipeline frame IDs, zero-based files
		return index if self.in_run(index) else None

	def dataset_time_origin(self) -> Optional[float]:
		return None   # a TUM run's own clock starts at 0


class CodaDatasetAssets(_RunFrames):
	kind = "coda"
	depth_scale = 1000.0   # FoundationStereo depth PNGs are millimetres
	default_max_depth = 30.0   # outdoor campus scenes

	def __init__(self, root: Path, sequence: str, fps: float, start_frame: int = 0, frame_count: Optional[int] = None, camera: str = "cam0"):
		super().__init__(fps, start_frame, frame_count)
		self.root = Path(root)
		self.sequence = str(sequence)
		self.camera = camera
		self.rgb = self._numbered(self.root / "2d_rect" / camera / self.sequence)
		self.depth = self._numbered(self.root / "3d_raw_estimated" / camera / self.sequence)
		self.stamps = self._stamps(self.root / "timestamps" / f"{self.sequence}.txt")
		self.frame_total = (max(self.rgb) + 1) if self.rgb else 0

	@staticmethod
	def _numbered(folder: Path) -> Dict[int, Path]:
		frames = {}
		if folder.is_dir():
			for path in folder.glob("*.png"):
				match = _TRAILING_NUMBER.search(path.stem)
				if match:
					frames[int(match.group(1))] = path
		return frames

	@staticmethod
	def _stamps(path: Path) -> List[float]:
		if not path.is_file():
			return []
		return [float(line) for line in path.read_text(encoding="utf-8").split() if line.strip()]

	def rgb_path(self, index: int) -> Optional[Path]:
		return self.rgb.get(index)

	def depth_path(self, index: int) -> Optional[Path]:
		return self.depth.get(index)

	def dataset_time_origin(self) -> Optional[float]:
		return self.stamps[0] if self.stamps else None

	def _stamp_matches(self, index: int, stamp: float) -> bool:
		return 0 <= index < len(self.stamps) and abs(self.stamps[index] - stamp) <= STAMP_TOLERANCE

	def description_index(self, source: Mapping[str, Any]) -> Optional[int]:
		frame_id, stamp = _frame_id(source), _stamp(source)
		candidate = self.start_frame + frame_id - 1 if frame_id is not None else None
		if stamp is None or not self.stamps:
			return candidate if candidate is not None and self.in_run(candidate) and candidate in self.rgb else None
		if candidate is not None and self.in_run(candidate) and self._stamp_matches(candidate, stamp):
			return candidate
		position = bisect.bisect_left(self.stamps, stamp - STAMP_TOLERANCE)
		for index in (position, position + 1):
			if self._stamp_matches(index, stamp) and self.in_run(index) and index in self.rgb:
				return index
		return None


def dataset_assets(kind: str, root: Path, *, sequence: str, fps: float, start_frame: int = 0, frame_count: Optional[int] = None):
	"""The adapter for an explicitly named dataset kind."""
	if kind == "tum":
		return TumDatasetAssets(root, fps=fps, start_frame=start_frame, frame_count=frame_count)
	if kind == "coda":
		return CodaDatasetAssets(root, sequence=sequence, fps=fps, start_frame=start_frame, frame_count=frame_count)
	raise ValueError(f"unsupported dataset kind {kind!r}: expected tum or coda")
