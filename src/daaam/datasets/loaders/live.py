"""Live dataset: RGB-D frames pulled from a running simulator while the pipeline maps them."""

import json
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from scipy.spatial.transform import Rotation

from daaam.datasets.interfaces import BaseDataset, DatasetExhausted, DatasetFrame
from daaam.datasets.live_protocol import DEFAULT_URL, DEPTH_SCALE, FrameClient, RemoteFrame, SourceClosed
from daaam.utils.transform import compute_ema_velocity

# Habitat world (x right, y up, the camera looks along -z at yaw 0) -> z-up world: (x, y, z) -> (x, -z, y).
_ZUP_FROM_YUP = np.array([[1.0, 0.0, 0.0, 0.0], [0.0, 0.0, -1.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0]])
# OpenGL camera (x right, y up, looks along -z) -> optical camera (x right, y down, looks along +z).
_OPTICAL_FROM_GL = np.diag([1.0, -1.0, -1.0, 1.0])
_UNBOUNDED = 10 ** 9  # a live stream has no length; the runner needs a number


def camera_pose_to_optical_zup(pose: np.ndarray, convention: str) -> np.ndarray:
	"""4x4 camera-to-world pose of a simulator -> the pipeline's convention (optical camera axes, z-up world).

	`opengl_yup` is Habitat's: an OpenGL camera in a y-up world (T_pipeline = Z @ T @ diag(1, -1, -1, 1), the same
	camera conversion as the HM3D loader's right-multiplication, plus the rotation of the world). `optical_zup` needs none.
	"""
	pose = np.asarray(pose, dtype=np.float64).reshape(4, 4)
	if convention == "optical_zup":
		return pose
	if convention == "opengl_yup":
		return _ZUP_FROM_YUP @ pose @ _OPTICAL_FROM_GL
	raise ValueError(f"unknown pose convention {convention!r}")


def matrix_to_pose7(matrix: np.ndarray) -> np.ndarray:
	"""4x4 transform -> [x, y, z, qx, qy, qz, qw]."""
	return np.concatenate([matrix[:3, 3], Rotation.from_matrix(matrix[:3, :3]).as_quat()])


def _put(values: List[Any], index: int, value: Any) -> None:
	"""values[index] = value, appending when index is the next free slot."""
	if index < len(values):
		values[index] = value
	else:
		values.append(value)


class SessionRecorder:
	"""Writes the mapped frames in the layout of scripts/prepare_tum_rgbd.py (rgb/, depth/, pose/poses.txt, timestamps.txt,
	camera_info.json).

	A recorded session can be mapped again offline (`--dataset-type ImageSequenceDataset`), post-processed by
	scripts/lift_native_places.py and opened in the replay workspace as a TUM-layout dataset. Frames are written by a
	background thread; writing a frame index again (the runner reads index 0 twice) replaces its files. While the session
	runs every pose is a file of its own (a crash leaves a folder that ImageSequenceDataset can still read); `finish()`
	merges them into pose/poses.txt.
	"""

	def __init__(self, root: Path):
		self.root = Path(root)
		self._queue: "queue.Queue[Optional[tuple]]" = queue.Queue(maxsize=64)
		self._thread: Optional[threading.Thread] = None
		self._error: Optional[BaseException] = None
		self.written = 0

	def start(self, info: Dict[str, Any]) -> None:
		if (self.root / "rgb").is_dir() and any((self.root / "rgb").iterdir()):
			raise FileExistsError(f"{self.root} already holds a recorded session: record into a new folder")
		for name in ("rgb", "depth", "pose"):
			(self.root / name).mkdir(parents=True, exist_ok=True)
		(self.root / "camera_info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
		self._thread = threading.Thread(target=self._run, name="live-recorder", daemon=True)
		self._thread.start()

	def _enqueue(self, item: Optional[tuple]) -> None:
		while True:
			if self._error is not None:
				raise RuntimeError(f"recording the session failed: {self._error}") from self._error
			try:
				self._queue.put(item, timeout=0.1)
				return
			except queue.Full:
				if self._thread is None or not self._thread.is_alive():
					raise RuntimeError("recording the session failed: writer stopped")

	def add(self, index: int, rgb: np.ndarray, depth_units: np.ndarray, pose: np.ndarray) -> None:
		self._enqueue((index, rgb, depth_units, pose))

	def finish(self, timestamps: List[float]) -> None:
		"""Wait for the writer, then merge the per-frame poses into pose/poses.txt (the layout of scripts/prepare_tum_rgbd.py)."""
		if self._thread is not None:
			try:
				self._enqueue(None)
			finally:
				self._thread.join()
				self._thread = None
			if self._error is not None:
				raise RuntimeError(f"recording the session failed: {self._error}") from self._error
			files = sorted(self.root.joinpath("pose").glob("[0-9]*.txt"))
			if files:
				rows = np.array([np.loadtxt(path).reshape(16) for path in files])
				np.savetxt(self.root / "pose" / "poses.txt", rows, fmt="%.12g")
				for path in files:
					path.unlink()
			(self.root / "timestamps.txt").write_text("".join(f"{t:.9f}\n" for t in timestamps), encoding="utf-8")

	def _run(self) -> None:
		while True:
			item = self._queue.get()
			if item is None:
				return
			index, rgb, depth, pose = item
			try:
				name = f"{index:06d}"
				for path, image in ((self.root / "rgb" / f"{name}.png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)),
						(self.root / "depth" / f"{name}.png", depth)):
					if not cv2.imwrite(str(path), image):
						raise OSError(f"cannot write {path}")
				np.savetxt(self.root / "pose" / f"{name}.txt", pose.reshape(1, 16), fmt="%.12g")
				self.written += 1
			except BaseException as error:  # noqa: BLE001 - reported by add() or finish()
				self._error = error
				return


class LiveDataset(BaseDataset):
	"""Frames from a simulator that someone is steering, mapped as they arrive.

	The runner reads `dataset[0]`, `dataset[1]`, ...; each read of a new index blocks until the simulator has captured
	a frame after the call (see live_protocol), so the map always follows the newest view and the simulator is never
	asked for frames the mapper cannot use. Reading the index that was read last returns the same frame again while it
	is at most `max_cached_age` seconds old; an older one is replaced by a fresh frame (the runner reads index 0 once to
	set up the camera and again as the first frame of the loop, tens of seconds later, after the models have loaded).
	Frame k is stamped k / fps, like every other dataset here, so a recorded session maps and replays as it did live.

	`data_path` is the folder that records the session (record=False: no recording). The stream ends, and the read
	that finds out raises DatasetExhausted, when the simulator closes or `request_stop()` is called (Ctrl-C).
	"""

	def __init__(
		self,
		data_path: Path,
		config: Optional[Dict[str, Any]] = None,
		url: str = DEFAULT_URL,
		fps: Optional[float] = None,
		record: bool = True,
		max_frames: Optional[int] = None,
		compute_velocities: bool = True,
		velocity_window: int = 10,
		velocity_alpha: float = 0.4,
		connect_timeout: float = 120.0,
		max_cached_age: float = 1.0,
		status_every: int = 20,
		client: Optional[Any] = None,
	):
		super().__init__(data_path, config)
		self._fps = float(fps if fps is not None else self.config.get("fps", 3.0))
		if not self._fps > 0:
			raise ValueError("fps must be positive")
		self.url = url
		self.max_frames = max_frames
		self.compute_velocities = compute_velocities
		self.velocity_alpha = velocity_alpha
		self.connect_timeout = connect_timeout
		self.max_cached_age = max_cached_age
		self.status_every = status_every
		self._client = client if client is not None else FrameClient(url)
		self._recorder = SessionRecorder(self.data_path) if record else None
		self._stop = threading.Event()
		self._connected = False
		self._closed = False
		self._camera_info: Optional[Dict[str, Any]] = None
		self._cached: Optional[DatasetFrame] = None
		self._cached_at = 0.0
		self._last_seq = 0
		self._rate = 0.0
		self._history: deque = deque(maxlen=velocity_window)  # (index, pose7, timestamp)
		# per frame index: time stamp, pose, seconds from capture to receipt, seconds blocked in the pull
		self._stamps: List[float] = []
		self._poses: List[np.ndarray] = []
		self._ages: List[float] = []
		self._waits: List[float] = []
		self._steps: List[float] = []  # metres between frame k and k + 1
		self._turns: List[float] = []  # degrees between frame k and k + 1
		self._started = 0.0
		self._returned = 0.0

	# --- BaseDataset ---

	def __len__(self) -> int:
		return self.max_frames if self.max_frames else _UNBOUNDED

	@property
	def fps(self) -> float:
		return self._fps

	def get_camera_info(self) -> Dict[str, Any]:
		if self._camera_info is None:
			self[0]
		return self._camera_info or {}

	def validate(self) -> bool:
		"""Connects to the simulator (waiting for it to come up); there are no files to check."""
		try:
			self._connect()
		except (TimeoutError, SourceClosed) as error:
			print(f"[live] {error}", file=sys.stderr, flush=True)
			return False
		return True

	def __getitem__(self, idx: int) -> DatasetFrame:
		if idx < 0 or idx >= len(self):
			raise IndexError(f"Index {idx} out of range [0, {len(self)})")
		cached = self._cached
		again = cached is not None and idx == cached.frame_id
		if again and time.monotonic() - self._cached_at <= self.max_cached_age:
			return cached
		if cached is not None and not again and idx != cached.frame_id + 1:
			print(f"[live] frame {idx} was read after frame {cached.frame_id}: a live stream only moves forward", file=sys.stderr, flush=True)
		raw, wait, age = self._pull()
		frame = self._convert(idx, raw)
		self._cached, self._cached_at = frame, time.monotonic()
		self._note(idx, frame, wait, age, again)
		if self.status_every and idx and idx % self.status_every == 0:
			self._print_status()
		return frame

	# --- control ---

	def request_stop(self) -> None:
		"""End the stream: the next (or the pending) read raises DatasetExhausted. Safe to call from a signal handler."""
		self._stop.set()

	def close(self) -> None:
		"""Finish the recording and write the session statistics (once)."""
		if self._closed:
			return
		self._closed = True
		self._client.close()
		if self._recorder is not None:
			self._recorder.finish(self._stamps)
			if self._recorder.root.is_dir():
				(self._recorder.root / "live_stats.json").write_text(json.dumps(self.stats(), indent=2), encoding="utf-8")

	def stats(self) -> Dict[str, Any]:
		"""What the session measured: speed of the mapper, age of the frames it got, motion between mapped frames."""
		def mean(values: List[float]) -> float:
			return float(np.mean(values)) if values else 0.0

		def percentile(values: List[float], q: float) -> float:
			return float(np.percentile(values, q)) if values else 0.0

		frames = len(self._stamps)
		wall = self._returned - self._started
		return {
			"frames": frames,
			"nominal_fps": self._fps,
			"mapped_fps": (frames - 1) / wall if frames > 1 and wall > 0 else 0.0,
			"frame_age_ms_mean": 1000 * mean(self._ages),
			"frame_age_ms_p95": 1000 * percentile(self._ages, 95),
			"capture_wait_ms_mean": 1000 * mean(self._waits),
			"step_m_mean": mean(self._steps),
			"step_m_max": max(self._steps, default=0.0),
			"turn_deg_mean": mean(self._turns),
			"turn_deg_max": max(self._turns, default=0.0),
		}

	# --- internals ---

	def _connect(self) -> None:
		if not self._connected:
			self._client.connect(timeout=self.connect_timeout, stop=self._stop)
			self._connected = True

	def _pull(self) -> Tuple[RemoteFrame, float, float]:
		"""The next captured frame, the seconds spent waiting for it and its age on arrival."""
		try:
			self._connect()
		except (TimeoutError, SourceClosed) as error:
			raise DatasetExhausted(str(error)) from error
		asked = time.monotonic()
		while True:
			if self._stop.is_set():
				raise DatasetExhausted("stopped")
			try:
				raw = self._client.request(self._last_seq, len(self._stamps), self._rate)
			except SourceClosed as error:
				raise DatasetExhausted(str(error)) from error
			if raw is not None:
				break
		self._last_seq = raw.seq
		return raw, time.monotonic() - asked, max(0.0, time.time() - raw.wall_time)

	def _convert(self, idx: int, raw: RemoteFrame) -> DatasetFrame:
		meta = raw.meta
		scale = float(meta.get("depth_scale", DEPTH_SCALE))
		height, width = raw.rgb.shape[:2]
		fx, fy, cx, cy = (float(v) for v in meta["intrinsics"])
		info = {
			"width": width,
			"height": height,
			"intrinsics": [[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]],
			"distortion": [0.0] * 5,
			"depth_scale": scale,
			"fps": self._fps,
			"source": meta.get("info", {}),
			"camera_axes": "+X right, +Y down, +Z forward",
			"world_axes": "+Z up",
		}
		matrix = camera_pose_to_optical_zup(np.asarray(meta["pose"]), meta.get("pose_convention", "optical_zup"))
		pose = matrix_to_pose7(matrix)
		timestamp = idx / self._fps
		if self._camera_info is None:
			self._camera_info = info
			if self._recorder is not None:
				self._recorder.start(info)
		rgb = np.array(raw.rgb)  # the wire buffer is read-only
		depth = raw.depth.astype(np.float32) / scale  # metres, 0 = no measurement
		lin_vel, ang_vel = np.zeros(3), np.zeros(3)
		if self.compute_velocities:
			lin_vel, ang_vel = self._velocity(idx, pose, timestamp)
		if self._recorder is not None:
			self._recorder.add(idx, rgb, np.array(raw.depth), matrix)
		return DatasetFrame(
			frame_id=idx, timestamp=timestamp, rgb_image=rgb, depth_image=depth, transform=pose,
			camera_info=info, lin_vel=lin_vel, ang_vel=ang_vel,
		)

	def _velocity(self, idx: int, pose: np.ndarray, timestamp: float) -> Tuple[np.ndarray, np.ndarray]:
		if self._history and self._history[-1][0] == idx:
			self._history.pop()
		self._history.append((idx, pose, timestamp))
		if len(self._history) < 2:
			return np.zeros(3), np.zeros(3)
		velocity = compute_ema_velocity(
			np.array([h[1] for h in self._history]), np.array([h[2] for h in self._history]), alpha=self.velocity_alpha,
		)
		return (velocity[1:4], velocity[4:7]) if velocity is not None else (np.zeros(3), np.zeros(3))

	def _note(self, idx: int, frame: DatasetFrame, wait: float, age: float, again: bool) -> None:
		"""Statistics per frame index; a frame read again replaces what its first read left."""
		now = time.monotonic()
		if idx == 0:
			self._started, self._rate = now, 0.0  # the clock of the run starts with the frame that the loop maps first
		elif now > self._returned:
			instant = 1.0 / (now - self._returned)
			self._rate = instant if self._rate == 0.0 else 0.8 * self._rate + 0.2 * instant
		self._returned = now
		_put(self._stamps, idx, frame.timestamp)
		_put(self._poses, idx, frame.transform)
		_put(self._ages, idx, age)
		_put(self._waits, idx, wait)
		if idx > 0 and idx <= len(self._poses) - 1:
			before, after = self._poses[idx - 1], self._poses[idx]
			_put(self._steps, idx - 1, float(np.linalg.norm(after[:3] - before[:3])))
			relative = Rotation.from_quat(before[3:]).inv() * Rotation.from_quat(after[3:])
			_put(self._turns, idx - 1, float(np.degrees(relative.magnitude())))

	def _print_status(self) -> None:
		if not self._steps:
			return
		recent = slice(-self.status_every, None)
		print(
			f"[live] frame {len(self._stamps) - 1}: {self._rate:.1f} frames/s mapped, frame age {1000 * np.mean(self._ages[recent]):.0f} ms, "
			f"motion between frames {np.mean(self._steps[recent]):.2f} m / {np.mean(self._turns[recent]):.1f} deg "
			f"(max {np.max(self._turns[recent]):.0f} deg)",
			flush=True,
		)
