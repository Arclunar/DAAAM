"""Fixture replay service for the real-browser tests; started inside WSL by tests/replay_browser_support.py.

    python3 -m tests.replay_web_fixture_server

It serves the repository's replay workspace on an ephemeral loopback port, with an output root and service state in a
temporary directory, and prints one JSON line {"base": URL, "root": DIR} when it listens. After that every stdin line
is one JSON command and gets one JSON reply line ({"error": ...} on failure); the commands build replay bundles with
the real finalizer and change files the way a pipeline or a processing job would. End of input stops the service and
deletes the fixtures. Diagnostics go to stderr only: stdout carries the protocol.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import cv2
import numpy as np

from scripts.replay_bundle import BundleOptions, finalize_bundle
from scripts.replay_web.server import ServerConfig, create_server

REPO = Path(__file__).resolve().parents[1]
CODA_ORIGIN = 1673884185.589118


def snapshot_arrays(frame: int, object_id: int, place_id: int, offset: float, fps: float, origin: float, rich: bool) -> Dict[str, np.ndarray]:
	"""The arrays of replay.extract_snapshot: a small tetrahedron mesh, objects, places and the camera."""
	arrays = {
		"mesh_pos": np.array([[0.0, 0.0, 0.0], [2.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 2.0]], np.float32),
		"mesh_col": np.array([[255, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 255]], np.uint8),
		"mesh_faces": np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], np.uint32),
		"place_edges": np.zeros((0, 2), np.int32),
		"room_place_edges": np.zeros((0, 2), np.int32),
		"bld_room_edges": np.zeros((0, 2), np.int32),
		"agent_quat": np.array([[1.0, 0.0, 0.0, 0.0]], np.float32),
		"agent_t": np.array([origin + frame / fps], np.float64),
		"place_t": np.array([origin + frame / fps], np.float64),
	}
	if rich:   # anchors that overlap on screen: three objects centimetres apart, two places, a room and a building
		lower = np.array([[0.0, 0.0, 0.0], [0.02, 0.01, 0.0], [0.04, 0.02, 0.0]], np.float32)
		arrays.update(
			obj_cid=np.array([11, 12, 13], np.int32), obj_label=np.array([42, 43, 44], np.uint32),
			obj_min=lower, obj_max=lower + np.array([0.4, 0.4, 0.6], np.float32),
			place_cid=np.array([3, 4], np.int32), place_pos=np.array([[0.0, 0.0, 0.0], [1.0, 1.0, 0.0]], np.float32),
			place_rad=np.array([0.2, 0.2], np.float32),
			room_pos=np.array([[0.5, 0.5, 0.0]], np.float32), bld_pos=np.array([[0.5, 0.5, 0.0]], np.float32),
			obj_place_edges=np.zeros((0, 2), np.int32), agent_pos=np.array([[0.0, 0.0, 0.0]], np.float32),
		)
	else:
		arrays.update(
			obj_cid=np.array([object_id], np.int32), obj_label=np.array([42], np.uint32),
			obj_min=np.array([[offset, 0.0, 0.0]], np.float32), obj_max=np.array([[offset + 0.4, 0.4, 0.6]], np.float32),
			place_cid=np.array([place_id], np.int32), place_pos=np.array([[offset, 0.0, 0.0]], np.float32),
			place_rad=np.array([0.2], np.float32), room_pos=np.zeros((0, 3), np.float32), bld_pos=np.zeros((0, 3), np.float32),
			obj_place_edges=np.array([[0, 0]], np.int32), agent_pos=np.array([[offset, 0.0, 0.0]], np.float32),
		)
	return arrays


def write_reference(path: Path, side: int = 30) -> Path:
	"""A flat side x side grid (two triangles per cell), the stand-in for the cached 1 cm reference mesh."""
	ys, xs = np.mgrid[0:side, 0:side]
	positions = np.stack([xs.ravel() * 0.05 - 0.5, ys.ravel() * 0.05 - 0.5, np.full(side * side, -0.1)], axis=1).astype(np.float32)
	index = np.arange(side * side).reshape(side, side)
	a, b, c, d = index[:-1, :-1].ravel(), index[:-1, 1:].ravel(), index[1:, :-1].ravel(), index[1:, 1:].ravel()
	np.savez(
		path,
		positions=positions,
		colors=np.tile(np.array([90, 140, 200], np.uint8), (side * side, 1)),
		normals=np.tile(np.array([0.0, 0.0, 1.0], np.float32), (side * side, 1)),
		triangles=np.concatenate([np.stack([a, b, c], axis=1), np.stack([b, d, c], axis=1)]).astype(np.uint32),
	)
	return path


def colour(index: int):
	return (min(255, 40 + index * 30), max(0, 230 - index * 25), 90)


class Fixtures:
	def __init__(self, root: Path):
		self.root = root
		self.output = root / "output"
		self.output.mkdir()
		self.count = 0

	def make_result(self, name: str = "tum_replay", kind: str = "tum", sequence: Optional[str] = None,
			snapshots: Optional[List[Dict[str, Any]]] = None, rich: bool = False, reference: bool = False,
			dataset: bool = True, frames: int = 2, description: bool = True, outside: bool = False,
			max_chunk_snapshots: int = 24, fps: Optional[float] = None, thumb_width: int = 64) -> Dict[str, Any]:
		"""A finalized result directory; `snapshots` lists {frame, object, place, offset} (default: frames 1 and 5)."""
		self.count += 1
		fps = fps or (15.0 if kind == "tum" else 10.0)
		sequence = sequence or ("fr1_desk" if kind == "tum" else "0")
		origin = 0.0 if kind == "tum" else CODA_ORIGIN
		work = self.root / "work" / ("%s_%d" % (name, self.count))
		recording = work / "recording"
		recording.mkdir(parents=True)
		for item in snapshots or [{"frame": 1, "object": 7, "place": 3, "offset": 0.0}, {"frame": 5, "object": 8, "place": 4, "offset": 1.0}]:
			np.savez(recording / ("snap_%06d.npz" % item["frame"]), **snapshot_arrays(
				item["frame"], item.get("object", 7), item.get("place", 3), item.get("offset", 0.0), fps, origin, rich))
		parent = (self.root / "elsewhere") if outside else self.output
		run = parent / name / ("out_20261017_%06d" % self.count)
		run.mkdir(parents=True)
		labels = [(42, "a green object"), (43, "a wooden desk"), (44, "a blue mug")] if rich else [(42, "a green object")]
		lines = ["label_names:"]
		for label, text in labels:
			lines += ["  - label: %d" % label, "    name: %s" % text]
			if description and label == 42:
				lines += ["    description_source:", "      frame_id: 1", "      timestamp: %.6f" % (origin + 1 / fps)]
		(run / "corrections.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
		root = self._dataset(work, kind, sequence, frames, fps) if dataset else None
		finalize_bundle(BundleOptions(
			recording=recording, source_graph=None, run_dir=run, output=run / "replay", dataset_kind=kind, sequence=sequence,
			dataset=root, fps=fps, thumb_width=thumb_width, max_chunk_snapshots=max_chunk_snapshots,
			reference_mesh=write_reference(work / "reference.npz") if reference else None,
			title="%s fixture %d" % (kind, self.count),
		))
		return {"path": str(run)}

	def _dataset(self, work: Path, kind: str, sequence: str, frames: int, fps: float) -> Path:
		if kind == "tum":
			root = work / "tum_dataset"
			(root / "rgb").mkdir(parents=True)
			(root / "depth").mkdir()
			for index in range(frames):
				cv2.imwrite(str(root / "rgb" / ("%06d.png" % index)), np.full((48, 64, 3), colour(index), np.uint8))
				cv2.imwrite(str(root / "depth" / ("%06d.png" % index)), np.full((48, 64), 5000 * (index + 1), np.uint16))
			(root / "camera_info.json").write_text(json.dumps({"depth_scale": 5000.0}), encoding="utf-8")
			return root
		root = work / "CODa"
		rgb = root / "2d_rect" / "cam0" / sequence
		depth = root / "3d_raw_estimated" / "cam0" / sequence
		rgb.mkdir(parents=True)
		depth.mkdir(parents=True)
		for index in range(frames):
			cv2.imwrite(str(rgb / ("2d_rect_cam0_%s_%d.png" % (sequence, index))), np.full((48, 64, 3), colour(index), np.uint8))
			cv2.imwrite(str(depth / ("depth_cam0_%s_%d.png" % (sequence, index))), np.full((48, 64), 2000 * (index + 1), np.uint16))
		(root / "timestamps").mkdir()
		(root / "timestamps" / ("%s.txt" % sequence)).write_text("".join("%.6f\n" % (CODA_ORIGIN + index / fps) for index in range(frames)), encoding="utf-8")
		return root

	def remove_bundle(self, path: str) -> Dict[str, Any]:
		shutil.rmtree(Path(path) / "replay")
		return {}

	def corrupt_chunk(self, path: str, index: int) -> Dict[str, Any]:
		"""Damage one timeline chunk without changing its size (its gzip check fails in the browser)."""
		manifest = json.loads((Path(path) / "replay" / "manifest.json").read_text(encoding="utf-8"))
		chunk = Path(path) / "replay" / manifest["chunks"][index]["path"]
		data = bytearray(chunk.read_bytes())
		for offset in range(10, len(data)):
			data[offset] ^= 0x5A
		chunk.write_bytes(bytes(data))
		return {"chunk": manifest["chunks"][index]["path"]}

	def exists(self, path: str) -> Dict[str, Any]:
		return {"manifest": (Path(path) / "replay" / "manifest.json").is_file(), "result": Path(path).is_dir()}


def main() -> int:
	root = Path(tempfile.mkdtemp(prefix="daaam-replay-browser-"))
	fixtures = Fixtures(root)
	server = create_server(ServerConfig(repo_root=REPO, output_root=fixtures.output, state_root=root / "state",
		distro=os.environ.get("WSL_DISTRO_NAME", ""), port=0))
	thread = threading.Thread(target=server.serve_forever, daemon=True)
	thread.start()
	commands: Dict[str, Callable[..., Dict[str, Any]]] = {
		"make_result": fixtures.make_result,
		"remove_bundle": fixtures.remove_bundle,
		"exists": fixtures.exists,
		"corrupt_chunk": fixtures.corrupt_chunk,
	}

	def reply(value: Dict[str, Any]) -> None:
		sys.stdout.write(json.dumps(value) + "\n")
		sys.stdout.flush()

	reply({"base": "http://127.0.0.1:%d" % server.server_address[1], "root": str(root)})
	try:
		for line in sys.stdin:
			if not line.strip():
				continue
			try:
				request = json.loads(line)
				handler = commands[request.pop("cmd")]
				reply({"ok": True, **handler(**request)})
			except Exception as error:   # report and keep serving
				traceback.print_exc()
				reply({"error": "%s: %s" % (type(error).__name__, error)})
	finally:
		server.shutdown()
		server.server_close()
		shutil.rmtree(root, ignore_errors=True)
	return 0


if __name__ == "__main__":
	sys.exit(main())
