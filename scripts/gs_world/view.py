"""Look at trained gs_world models in the browser: orbit, pan, zoom and fly through the splats like a 3D model.

	docker compose run --rm -T daaam-ws python -m scripts.gs_world.view latest       (just gs-view)
	... view all                                                                      every model, side by side

then open http://127.0.0.1:8770/ . Every frame is rendered on the GPU by the same gsplat call that gs_world.render
uses for the test sequences, so the view is exactly what a rendered sequence would contain, artifacts away from the
recorded cameras included; the page shows how far the current camera is from the nearest recorded one. The server
sends JPEG frames to a page that needs neither network access nor JavaScript libraries and listens on the loopback
address only. The models stay on the GPU while the viewer runs: Ctrl+C stops it, and it stops itself after
--idle-timeout minutes without a request.

Models of several scenes can be shown together (scenes.py): the page lists the models on disk, and the ones that are ticked
stand side by side in one world; a single model is shown in the world frame of its dataset.

Endpoints (all GET): / (the page), /info (the scenes on screen and their recorded cameras), /frame (a JPEG; the camera is
an orbit camera tx,ty,tz,yaw,pitch,dist or a recorded camera cam=N, plus w,h,vfov,path), /pick (the 3D point under an image
position px,py, returned as the orbit that looks at it), /scenes (the models on disk and which are on screen) and
/select?ids=a,b (put those models on screen, in that order).
"""

import argparse
import json
import math
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import parse_qs, urlsplit

import cv2
import numpy as np

from . import env
from .data import Camera
from .geometry import (estimate_up, invert, nearest_pose, orbit_from_eye_target, orbit_pose, pose_to_orbit, up_basis,
	vertical_fov_deg, view_intrinsics)
from .scenes import MAX_SCENES, SceneEntry, SceneError, pick_models

PAGE = Path(__file__).with_name("viewer.html")
MIN_SIZE = 16
MAX_PIXELS = 4_000_000
PATH_COLOR = (40, 150, 255)  # BGR: recorded training cameras
HELD_OUT_COLOR = (230, 200, 60)  # BGR: recorded cameras that were held out of the training
BACKGROUND = (0.09, 0.09, 0.11)

# renderer(T_world_camera, width, height, vertical fov in degrees) -> (RGB uint8 [H, W, 3], depth in metres [H, W], alpha [H, W])
Renderer = Callable[[np.ndarray, int, int, float], Tuple[np.ndarray, np.ndarray, np.ndarray]]


@dataclass(frozen=True)
class ViewRequest:
	width: int
	height: int
	vfov: float
	camera: Optional[int]  # a recorded camera shown exactly, or None for the orbit camera
	target: Tuple[float, float, float]
	yaw: float
	pitch: float
	dist: float
	path: bool  # draw the recorded camera path
	pixel: Optional[Tuple[float, float]] = None  # /pick: image position in continuous pixel coordinates


def _single(query: Dict[str, List[str]], name: str) -> Optional[str]:
	values = query.get(name)
	if not values:
		return None
	if len(values) != 1:
		raise ValueError(f"{name} is given more than once")
	return values[0]


def _number(query: Dict[str, List[str]], name: str, default: Optional[float], lo: Optional[float] = None,
		hi: Optional[float] = None) -> float:
	text = _single(query, name)
	if text is None:
		if default is None:
			raise ValueError(f"{name} is required")
		return float(default)
	try:
		value = float(text)
	except ValueError:
		raise ValueError(f"{name} must be a number, got {text!r}") from None
	if not math.isfinite(value):
		raise ValueError(f"{name} must be finite")
	if (lo is not None and value < lo) or (hi is not None and value > hi):
		raise ValueError(f"{name} must be between {lo} and {hi}, got {value:g}")
	return value


def project_points(points: np.ndarray, T_world_camera: np.ndarray, K: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
	"""(continuous pixel coordinates [n, 2], depth along the optical axis [n]) of world points seen from a camera."""
	T = np.asarray(T_world_camera, dtype=np.float64)
	camera_points = (np.asarray(points, dtype=np.float64) - T[:3, 3]) @ T[:3, :3]
	z = camera_points[:, 2]
	safe = np.where(np.abs(z) < 1e-9, 1e-9, z)
	uv = np.stack([K[0, 0] * camera_points[:, 0] / safe + K[0, 2], K[1, 1] * camera_points[:, 1] / safe + K[1, 2]], axis=1)
	return uv, z


def draw_path(image: np.ndarray, T_world_camera: np.ndarray, K: np.ndarray, centers: np.ndarray, forwards: np.ndarray,
		held_out: np.ndarray, highlight: Optional[int] = None, tick: float = 0.12, joined: Optional[np.ndarray] = None) -> None:
	"""Draw the recorded camera path into a BGR image (in place).

	A line through the camera centres (`joined[i]`: whether camera i and i + 1 are connected, default all; the last
	camera of one scene is not connected to the first of the next), a dot per camera (another colour for the held-out
	ones), a short line along the viewing direction of every 10th camera, and a ring around the camera `highlight`.
	Cameras behind the viewer are skipped.
	"""
	height, width = image.shape[:2]
	uv, z = project_points(centers, T_world_camera, K)
	pixels = np.round(uv - 0.5)
	visible = (z > 0.05) & np.all(np.abs(pixels) < 4 * max(width, height), axis=1)
	pixels = pixels.astype(np.int32)
	link = visible[:-1] & visible[1:]
	if joined is not None:
		link &= np.asarray(joined, dtype=bool)
	both = np.flatnonzero(link)
	if len(both):
		cv2.polylines(image, list(np.stack([pixels[both], pixels[both + 1]], axis=1)), False, PATH_COLOR, 1, cv2.LINE_AA)
	inside = visible & (pixels[:, 0] >= 0) & (pixels[:, 0] < width) & (pixels[:, 1] >= 0) & (pixels[:, 1] < height)
	for i in np.flatnonzero(inside):
		cv2.circle(image, (int(pixels[i, 0]), int(pixels[i, 1])), 2, HELD_OUT_COLOR if held_out[i] else PATH_COLOR, -1,
			cv2.LINE_AA)
	ends, end_z = project_points(centers + tick * forwards, T_world_camera, K)
	ends = np.round(ends - 0.5)
	for i in np.flatnonzero(visible & (end_z > 0.05))[::10]:
		if np.all(np.abs(ends[i]) < 4 * max(width, height)):
			cv2.line(image, (int(pixels[i, 0]), int(pixels[i, 1])), (int(ends[i, 0]), int(ends[i, 1])), PATH_COLOR, 1,
				cv2.LINE_AA)
	if highlight is not None and inside[highlight]:
		cv2.circle(image, (int(pixels[highlight, 0]), int(pixels[highlight, 1])), 7, (255, 255, 255), 1, cv2.LINE_AA)


def draw_scene_labels(image: np.ndarray, T_world_camera: np.ndarray, K: np.ndarray, anchors: np.ndarray,
		names: Sequence[str]) -> None:
	"""Write each scene's name (white with a dark outline, ASCII) above its anchor point, if that is in front of the camera."""
	height, width = image.shape[:2]
	uv, z = project_points(anchors, T_world_camera, K)
	for (u, v), depth, name in zip(uv, z, names):
		if depth <= 0.3 or not (-width < u < 2 * width and -height < v < 2 * height):
			continue
		(text_width, _), _ = cv2.getTextSize(name, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)
		origin = (int(u - text_width / 2), int(v))
		cv2.putText(image, name, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3, cv2.LINE_AA)
		cv2.putText(image, name, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)


def pivot_depth(depth: np.ndarray, alpha: np.ndarray) -> Optional[float]:
	"""Median depth of the opaque pixels in the middle of the image: where an orbit around that view would pivot."""
	h, w = depth.shape
	centre = (slice(h // 4, h - h // 4), slice(w // 4, w - w // 4))
	valid = (alpha[centre] > 0.5) & (depth[centre] > 0)
	if int(valid.sum()) < 20:
		return None
	return float(np.median(depth[centre][valid]))


class ViewerApp:
	"""What the viewer shows: the renderer, the recorded cameras and the page. The HTTP handler only calls its methods.

	`scenes` says which of the poses belong to which scene when several models are shown together: dicts with id, name,
	start and count (the poses of the scene), held_out (their local indices) and gaussians. Without it all poses are one scene.
	"""

	def __init__(self, renderer: Renderer, poses: np.ndarray, camera: Camera, *, title: str, gaussians: int,
			held_out: Sequence[int] = (), up: Sequence[float] = (0.0, 0.0, 1.0), max_width: int = 1600,
			quality: int = 88, page: Union[bytes, Path] = b"", scenes: Optional[Sequence[Dict[str, Any]]] = None):
		self.renderer = renderer
		self.poses = np.asarray(poses, dtype=np.float64)
		if self.poses.ndim != 3 or self.poses.shape[1:] != (4, 4) or len(self.poses) == 0:
			raise ValueError("poses must be a non-empty [N, 4, 4] array")
		self.camera = camera
		self.title = title
		self.gaussians = int(gaussians)
		self.up = np.asarray(up, dtype=np.float64) / np.linalg.norm(up)
		self.max_width = int(max_width)
		self.quality = int(quality)
		self.page = page
		self.vfov = vertical_fov_deg(camera.K, camera.height)
		self.centers = self.poses[:, :3, 3]
		self.forwards = self.poses[:, :3, 2]
		if scenes is None:
			scenes = [{"id": "", "name": title, "start": 0, "count": len(self.poses), "held_out": list(held_out),
				"gaussians": int(gaussians)}]
		self.scenes = [dict(scene) for scene in scenes]
		if sum(int(scene["count"]) for scene in self.scenes) != len(self.poses):
			raise ValueError("the scenes must cover the poses exactly")
		self.scene_of = np.repeat(np.arange(len(self.scenes)), [int(scene["count"]) for scene in self.scenes])
		self.held_out = sorted(int(scene["start"]) + int(i) for scene in self.scenes for i in scene["held_out"]
			if 0 <= int(i) < int(scene["count"]))
		self.scene_anchors = np.array([self._anchor(scene) for scene in self.scenes])
		self.held_out_mask = np.zeros(len(self.poses), dtype=bool)
		self.held_out_mask[self.held_out] = True
		# the extrapolation distance is measured to the cameras the model was trained on
		self.trained = np.flatnonzero(~self.held_out_mask) if (~self.held_out_mask).any() else np.arange(len(self.poses))
		self.trained_poses = self.poses[self.trained]
		self.gpu = threading.Lock()  # one render at a time
		self.last_request = time.monotonic()
		self.initial = self._initial_view()
		self.overview = self._overview_view()

	def page_bytes(self) -> bytes:
		"""The page; a path is read on every request, so the file can be edited while the viewer runs."""
		return self.page.read_bytes() if isinstance(self.page, Path) else self.page

	def touch(self) -> None:
		self.last_request = time.monotonic()

	def idle_seconds(self) -> float:
		return time.monotonic() - self.last_request

	def _render(self, T: np.ndarray, width: int, height: int, vfov: float):
		with self.gpu:
			return self.renderer(T, width, height, vfov)

	def _initial_view(self) -> Dict[str, object]:
		"""Orbit parameters of the first recorded camera, pivoting at the median depth in front of it."""
		width = 320
		height = max(MIN_SIZE, int(round(width * self.camera.height / self.camera.width)))
		_, depth, alpha = self._render(self.poses[0], width, height, self.vfov)
		pivot = pivot_depth(depth, alpha) or 2.0
		return pose_to_orbit(self.poses[0], pivot, self.up)

	def _overview_view(self) -> Dict[str, object]:
		"""A bird's-eye orbit over what the recorded cameras look at, far enough away to see all of it and the path.

		Every scene gets a circle around what its cameras look at, as wide as most of that and of its camera path; the view
		takes in all circles (for one scene: its circle). Scenes that stand in a row are seen from the side, the first on the left.
		"""
		width = 160
		height = max(MIN_SIZE, int(round(width * self.camera.height / self.camera.width)))
		per_scene = max(8, 48 // len(self.scenes))
		circles = []
		for scene in self.scenes:
			start, count = int(scene["start"]), int(scene["count"])
			looked_at = []
			for i in start + np.unique(np.linspace(0, count - 1, min(per_scene, count)).round().astype(int)):
				_, depth, alpha = self._render(self.poses[i], width, height, self.vfov)
				pivot = pivot_depth(depth, alpha)
				if pivot is not None:
					looked_at.append(self.centers[i] + pivot * self.forwards[i])
			centers = self.centers[start:start + count]
			middle = np.median(looked_at, axis=0) if looked_at else np.median(centers, axis=0)
			cloud = np.vstack([np.asarray(looked_at).reshape(-1, 3), centers])
			circles.append((middle, float(np.percentile(np.linalg.norm(cloud - middle, axis=1), 90))))
		focus = (np.min([c - r for c, r in circles], axis=0) + np.max([c + r for c, r in circles], axis=0)) / 2.0
		radius = max(float(np.linalg.norm(c - focus)) + r for c, r in circles)
		dist = max(2.5, 1.25 * radius / math.tan(math.radians(self.vfov) / 2.0))
		yaw = float(self.initial["yaw"])
		if len(circles) > 1:
			e1, e2, u = up_basis(self.up)
			along = circles[-1][0] - circles[0][0]
			along = along - np.dot(along, u) * u  # the way the row runs, level
			if np.linalg.norm(along) > 1e-6:
				away = np.cross(along / np.linalg.norm(along), u)  # the side to look from: the row then runs to the right
				yaw = float(np.arctan2(away @ e2, away @ e1))
		return {"target": [float(v) for v in focus], "yaw": yaw, "pitch": 0.9, "dist": dist}

	def _anchor(self, scene: Dict[str, Any]) -> List[float]:
		"""Where a scene's name floats: over the middle of its camera path."""
		centers = self.centers[int(scene["start"]):int(scene["start"]) + int(scene["count"])]
		return [float(np.median(centers[:, 0])), float(np.median(centers[:, 1])), float(np.percentile(centers[:, 2], 95)) + 1.2]

	def locate(self, index: int) -> Tuple[str, int]:
		"""(scene name, index within that scene) of a recorded camera."""
		scene = self.scenes[int(self.scene_of[index])]
		return str(scene["name"]), int(index) - int(scene["start"])

	def info(self) -> Dict[str, object]:
		self.touch()
		return {
			"title": self.title,
			"gaussians": self.gaussians,
			"scenes": [{"id": s["id"], "name": s["name"], "start": s["start"], "count": s["count"], "gaussians": s["gaussians"]}
				for s in self.scenes],
			"cameras": len(self.poses),
			"held_out": self.held_out,
			"width": self.camera.width,
			"height": self.camera.height,
			"vfov": round(self.vfov, 3),
			"max_width": self.max_width,
			"up": [float(v) for v in self.up],
			"initial": self.initial,
			"overview": self.overview,
		}

	def parse(self, query: Dict[str, List[str]], pick: bool = False) -> ViewRequest:
		"""A ViewRequest from query parameters; ValueError (with a message for the client) when they are unusable."""
		width = int(round(_number(query, "w", None, MIN_SIZE, self.max_width)))
		height = int(round(_number(query, "h", None, MIN_SIZE, self.max_width)))
		if width * height > MAX_PIXELS:
			raise ValueError(f"w x h must not exceed {MAX_PIXELS} pixels")
		vfov = _number(query, "vfov", self.vfov, 10.0, 140.0)
		camera = None
		if _single(query, "cam") is not None:
			camera = int(_number(query, "cam", None, 0, len(self.poses) - 1))
		initial = self.initial
		target = tuple(_number(query, name, initial["target"][i]) for i, name in enumerate(("tx", "ty", "tz")))
		yaw = _number(query, "yaw", initial["yaw"])
		pitch = _number(query, "pitch", initial["pitch"])
		dist = _number(query, "dist", initial["dist"], 0.005, 5000.0)
		path = _single(query, "path") in ("1", "true")
		pixel = None
		if pick:
			pixel = (_number(query, "px", None, 0.0, float(width)), _number(query, "py", None, 0.0, float(height)))
		return ViewRequest(width, height, vfov, camera, target, yaw, pitch, dist, path, pixel)

	def _pose(self, req: ViewRequest) -> np.ndarray:
		if req.camera is not None:
			return self.poses[req.camera]
		return orbit_pose(req.target, req.yaw, req.pitch, req.dist, self.up)

	def frame(self, req: ViewRequest) -> Tuple[bytes, Dict[str, object]]:
		"""(JPEG bytes, information about the view) for a request."""
		self.touch()
		T = self._pose(req)
		started = time.perf_counter()
		rgb, depth, alpha = self._render(T, req.width, req.height, req.vfov)
		rendered = time.perf_counter()
		if req.camera is not None:
			orbit = pose_to_orbit(T, pivot_depth(depth, alpha) or float(self.initial["dist"]), self.up)
		else:
			orbit = {"target": list(req.target), "yaw": req.yaw, "pitch": req.pitch, "dist": req.dist}
		nearest, distance, angle = nearest_pose(self.trained_poses, T)
		nearest = int(self.trained[nearest])
		image = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2BGR)
		if req.path:
			K = view_intrinsics(req.width, req.height, req.vfov)
			draw_path(image, T, K, self.centers, self.forwards, self.held_out_mask, nearest, joined=self.scene_of[:-1] == self.scene_of[1:])
			if len(self.scenes) > 1:
				draw_scene_labels(image, T, K, self.scene_anchors, [str(s["name"]) for s in self.scenes])
		ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
		if not ok:
			raise RuntimeError("JPEG encoding failed")
		encoded = time.perf_counter()
		nearest_scene, nearest_local = self.locate(nearest)
		camera_scene, camera_local = self.locate(req.camera) if req.camera is not None else (None, None)
		info = {
			"eye": T[:3, 3].tolist(), "right": T[:3, 0].tolist(), "down": T[:3, 1].tolist(), "fwd": T[:3, 2].tolist(),
			"orbit": orbit,
			"camera": req.camera,
			"camera_scene": camera_scene,
			"camera_local": camera_local,
			"camera_held_out": bool(req.camera is not None and self.held_out_mask[req.camera]),
			"nearest": {"index": nearest, "scene": nearest_scene, "local": nearest_local, "distance": distance, "angle": angle},
			"ms": {"render": round((rendered - started) * 1000, 1), "encode": round((encoded - rendered) * 1000, 1)},
		}
		return buffer.tobytes(), info

	def pick(self, req: ViewRequest) -> Dict[str, object]:
		"""The surface point under an image position, as the orbit (same eye) that looks at it; {"ok": False} for empty space."""
		self.touch()
		T = self._pose(req)
		_, depth, alpha = self._render(T, req.width, req.height, req.vfov)
		px, py = req.pixel
		x, y = min(int(px), req.width - 1), min(int(py), req.height - 1)
		rows, cols = slice(max(0, y - 3), y + 4), slice(max(0, x - 3), x + 4)
		valid = (alpha[rows, cols] > 0.5) & (depth[rows, cols] > 0)
		if not valid.any():
			return {"ok": False}
		z = float(np.median(depth[rows, cols][valid]))
		K = view_intrinsics(req.width, req.height, req.vfov)
		camera_point = np.array([(px - K[0, 2]) / K[0, 0] * z, (py - K[1, 2]) / K[1, 1] * z, z])
		point = T[:3, :3] @ camera_point + T[:3, 3]
		return {"ok": True, "point": point.tolist(), "depth": z, "orbit": orbit_from_eye_target(T[:3, 3], point, self.up)}


class ViewerHub:
	"""The world on screen and the scenes that could be on screen: select() swaps the world for another set of scenes.

	`catalog()` lists the models on disk (SceneEntry; it is called again for every request, so new models show up) and
	`build(entries)` makes the ViewerApp that shows a selection of them. Without them the hub has one fixed app and
	select() refuses. `on_swap()` runs after the old app has been dropped (to give its GPU memory back).
	"""

	def __init__(self, app: ViewerApp, selected: Sequence[str] = (),
			catalog: Optional[Callable[[], Sequence[SceneEntry]]] = None,
			build: Optional[Callable[[Sequence[SceneEntry]], ViewerApp]] = None,
			on_swap: Optional[Callable[[], None]] = None):
		self.app = app
		self.selected = list(selected)
		self._catalog = catalog
		self._build = build
		self._on_swap = on_swap
		self._lock = threading.Lock()  # one swap at a time

	def touch(self) -> None:
		self.app.touch()

	def idle_seconds(self) -> float:
		return self.app.idle_seconds()

	def scenes(self) -> Dict[str, object]:
		"""The models on disk and which of them are on screen."""
		self.touch()
		models = [entry.to_json() for entry in self._catalog()] if self._catalog is not None else []
		return {"models": models, "selected": list(self.selected), "max_scenes": MAX_SCENES,
			"selectable": self._catalog is not None and self._build is not None}

	def select(self, ids: Sequence[str]) -> ViewerApp:
		"""Put the models with these ids on screen, in this order (ValueError, KeyError for an unknown id, SceneError)."""
		self.touch()
		if self._catalog is None or self._build is None:
			raise ValueError("this viewer cannot change its scenes")
		entries = pick_models(list(self._catalog()), list(ids))
		with self._lock:
			if [entry.id for entry in entries] == self.selected:
				return self.app
			app = self._build(entries)
			previous, self.app, self.selected = self.app, app, [entry.id for entry in entries]
		del previous
		if self._on_swap is not None:
			self._on_swap()
		return self.app


class Handler(BaseHTTPRequestHandler):
	"""GET only; the subclass made by make_server() carries the hub and the Host values that are accepted."""

	hub: ViewerHub
	allowed_hosts: frozenset = frozenset()
	protocol_version = "HTTP/1.1"
	server_version = "gs-viewer"

	def log_message(self, format, *args) -> None:  # a frame request per mouse move would flood the terminal
		pass

	def _send(self, status: int, body: bytes, content_type: str, headers: Optional[Dict[str, str]] = None) -> None:
		self.send_response(status)
		self.send_header("Content-Type", content_type)
		self.send_header("Content-Length", str(len(body)))
		self.send_header("Cache-Control", "no-store")
		for name, value in (headers or {}).items():
			self.send_header(name, value)
		self.end_headers()
		self.wfile.write(body)

	def _json(self, status: int, payload: Dict[str, object]) -> None:
		self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

	def do_GET(self) -> None:
		try:
			self._route()
		except (BrokenPipeError, ConnectionResetError):
			pass
		except Exception as exc:  # a failed render must not take the viewer down
			traceback.print_exc()
			try:
				self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
			except OSError:
				pass

	def _route(self) -> None:
		if self.headers.get("Host", "") not in self.allowed_hosts:
			self._json(403, {"error": "unexpected Host header: open the viewer through its loopback address"})
			return
		url = urlsplit(self.path)
		query = parse_qs(url.query, keep_blank_values=True)
		app = self.hub.app  # one request sees one world, even when another request swaps it
		try:
			if url.path in ("/", "/index.html"):
				app.touch()
				self._send(200, app.page_bytes(), "text/html; charset=utf-8")
			elif url.path == "/info":
				self._json(200, app.info())
			elif url.path == "/frame":
				jpeg, info = app.frame(app.parse(query))
				self._send(200, jpeg, "image/jpeg", {"X-GS-Info": json.dumps(info, separators=(",", ":"))})
			elif url.path == "/pick":
				self._json(200, app.pick(app.parse(query, pick=True)))
			elif url.path == "/scenes":
				self._json(200, self.hub.scenes())
			elif url.path == "/select":
				ids = [item for item in (_single(query, "ids") or "").split(",") if item]
				app = self.hub.select(ids)
				self._json(200, {"ok": True, "selected": list(self.hub.selected), "info": app.info()})
			else:
				self._json(404, {"error": f"no such page: {url.path}"})
		except ValueError as exc:
			self._json(400, {"error": str(exc)})
		except KeyError as exc:
			self._json(404, {"error": str(exc.args[0]) if exc.args else "not found"})
		except SceneError as exc:
			self._json(500, {"error": str(exc)})


class ViewerServer(ThreadingHTTPServer):
	daemon_threads = True  # an open connection never keeps the process alive
	allow_reuse_address = True


def make_server(target: Union[ViewerApp, ViewerHub], host: str = "127.0.0.1", port: int = 8770) -> ViewerServer:
	"""An HTTP server for an app (or a hub of apps); port 0 picks a free port (see server.server_address)."""
	class BoundHandler(Handler):
		pass

	hub = target if isinstance(target, ViewerHub) else ViewerHub(target)
	BoundHandler.hub = hub
	server = ViewerServer((host, port), BoundHandler)
	server.hub = hub
	bound = server.server_address[1]
	names = {host, "127.0.0.1", "localhost", "[::1]"}
	BoundHandler.allowed_hosts = frozenset(f"{name}:{bound}" for name in names)
	return server


def serve(server: ViewerServer, app: Union[ViewerApp, ViewerHub], idle_seconds: float = 0.0) -> int:
	"""Serve until Ctrl+C, SIGTERM or idle_seconds without a request (0: never idle out)."""
	def stop(*_args) -> None:
		threading.Thread(target=server.shutdown, daemon=True).start()

	try:
		signal.signal(signal.SIGTERM, stop)
	except ValueError:  # not the main thread (tests)
		pass
	if idle_seconds > 0:
		def watch() -> None:
			while True:
				time.sleep(min(1.0, idle_seconds / 4))
				if app.idle_seconds() > idle_seconds:
					print(f"no request for {idle_seconds / 60:g} min: the viewer stops and frees the GPU", flush=True)
					server.shutdown()
					return

		threading.Thread(target=watch, daemon=True).start()
	try:
		server.serve_forever(poll_interval=0.2)
	except KeyboardInterrupt:
		pass
	finally:
		server.server_close()
	return 0


class GsplatRenderer:
	"""The renderer of gs_world.render: antialiased gsplat rasterization at the model's SH degree, no exposure change."""

	def __init__(self, gsplat, params, sh_degree: int, device: str = "cuda", background: Sequence[float] = BACKGROUND):
		import torch

		self.torch, self.gsplat, self.params, self.sh_degree, self.device = torch, gsplat, params, sh_degree, device
		self.background = torch.tensor([list(background)], dtype=torch.float32, device=device)

	def __call__(self, T: np.ndarray, width: int, height: int, vfov: float):
		from .model import render

		torch = self.torch
		K = torch.from_numpy(view_intrinsics(width, height, vfov)).float().to(self.device)[None]
		viewmat = torch.from_numpy(invert(T)).float().to(self.device)[None]
		with torch.no_grad():
			rgb, depth, alpha, _ = render(self.gsplat, self.params, viewmat, K, width, height, self.sh_degree,
				with_depth=True, backgrounds=self.background)
			image = (rgb[0].clamp(0, 1) * 255).round().to(torch.uint8).cpu().numpy()
			return image, depth[0].float().cpu().numpy(), alpha[0].float().cpu().numpy()


def parse_up(text: str) -> np.ndarray:
	try:
		values = [float(v) for v in text.split(",")]
	except ValueError:
		values = []
	if len(values) != 3 or not all(math.isfinite(v) for v in values) or not any(values):
		raise SystemExit(f"--up needs three comma separated numbers that are not all zero, or 'auto', got {text!r}")
	return np.asarray(values)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
	p = argparse.ArgumentParser(description="Look at trained gs_world models in the browser.",
		formatter_class=argparse.ArgumentDefaultsHelpFormatter)
	p.add_argument("models", nargs="*", metavar="MODEL",
		help="what to show first: 'latest', 'all' (every model, side by side), a model id <dataset>/<time> or a model folder "
		"(with model.pt); several are shown side by side (default: latest)")
	p.add_argument("--host", default="127.0.0.1", help="address to listen on (the loopback default is reachable from this computer only)")
	p.add_argument("--port", type=int, default=8770)
	p.add_argument("--max-width", type=int, default=1600, help="widest image the page may ask for, in pixels")
	p.add_argument("--quality", type=int, default=88, help="JPEG quality of the frames, 30-100")
	p.add_argument("--up", default="0,0,1",
		help="world up direction x,y,z (DAAAM worlds are z-up), or 'auto': the mean up of the recorded cameras (of the first scene)")
	p.add_argument("--idle-timeout", type=float, default=30.0,
		help="stop after this many minutes without a request (0: never); the models keep GPU memory while it runs")
	args = p.parse_args(argv)
	if not args.models:
		args.models = ["latest"]
	if not 30 <= args.quality <= 100:
		p.error("--quality must be between 30 and 100")
	if not MIN_SIZE <= args.max_width <= 4096:
		p.error(f"--max-width must be between {MIN_SIZE} and 4096")
	return args


def choose_scenes(specs: Sequence[str], entries: Sequence[SceneEntry]) -> Tuple[List[SceneEntry], List[SceneEntry]]:
	"""(scenes to show first, catalog entries for models outside the models folder) from the command line words."""
	from .scenes import entry_for_folder

	by_id = {entry.id: entry for entry in entries}
	chosen: List[SceneEntry] = []
	extras: List[SceneEntry] = []
	for spec in specs:
		if spec == "latest":
			if not entries:
				raise SystemExit("there is no model to show yet: train one with just gs-train")
			found = [entries[0]]
		elif spec == "all":
			found = sorted(entries, key=lambda entry: entry.id)
		elif spec in by_id:
			found = [by_id[spec]]
		elif (Path(spec) / "model.pt").is_file():
			entry = entry_for_folder(Path(spec))
			found = [entry]
			if entry.id not in by_id:
				extras.append(entry)
		else:
			raise SystemExit(f"{spec!r} is not a model id, a model folder or 'latest' / 'all'; the models: "
				f"{', '.join(sorted(by_id)) or 'none'}")
		for entry in found:
			if entry.id not in {c.id for c in chosen}:
				chosen.append(entry)
	if len(chosen) > MAX_SCENES:
		raise SystemExit(f"at most {MAX_SCENES} scenes can be shown together, {len(chosen)} were asked for")
	return chosen, extras


def main(argv: Optional[Sequence[str]] = None) -> int:
	args = parse_args(argv)
	gsplat = env.import_gsplat()
	import gc

	import torch

	from . import scenes as scn
	from .train import OUTPUT_ROOT

	if not torch.cuda.is_available():
		raise SystemExit("CUDA is not available")
	root = OUTPUT_ROOT / "models"  # where gs_world.train writes the models the page lists
	chosen, extras = choose_scenes(args.models, scn.scan_models(root))

	def catalog() -> List[SceneEntry]:
		entries = scn.scan_models(root)
		return entries + [extra for extra in extras if extra.id not in {entry.id for entry in entries}]

	def build(entries: Sequence[SceneEntry]) -> ViewerApp:
		combined = scn.combine(scn.load_models(entries))
		params = {key: value.to("cuda") for key, value in combined.params.items()}
		names = [str(scene["name"]) for scene in combined.scenes]
		title = f"{entries[0].dataset} / {entries[0].time}" if len(entries) == 1 else scn.title_of(names)
		up = estimate_up(combined.poses[:combined.scenes[0]["count"]]) if args.up == "auto" else parse_up(args.up)
		return ViewerApp(GsplatRenderer(gsplat, params, combined.sh_degree), combined.poses, combined.camera, title=title,
			gaussians=combined.gaussians, up=up, max_width=args.max_width, quality=args.quality, page=PAGE,
			scenes=combined.scenes)

	def release() -> None:
		gc.collect()
		torch.cuda.empty_cache()

	app = build(chosen)
	hub = ViewerHub(app, [entry.id for entry in chosen], catalog, build, release)
	try:
		server = make_server(hub, args.host, args.port)
	except OSError as exc:
		raise SystemExit(f"cannot listen on {args.host}:{args.port}: {exc} (another viewer running? pick --port)") from exc
	print(f"showing {app.title}: {app.gaussians} Gaussians, {len(app.poses)} recorded cameras "
		f"({len(app.held_out)} held out), up {np.round(app.up, 3).tolist()}; {len(catalog())} model(s) to choose from")
	print(f"viewer: http://{args.host}:{server.server_address[1]}/  (Ctrl+C stops it; it holds the models on the GPU, so "
		"stop it before a DAAAM run)", flush=True)
	return serve(server, hub, args.idle_timeout * 60)


if __name__ == "__main__":
	sys.exit(main())
