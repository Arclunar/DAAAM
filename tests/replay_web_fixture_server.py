"""Fixture replay service for the real-browser tests; started inside WSL by tests/replay_browser_support.py.

    python3 -m tests.replay_web_fixture_server

It serves the repository's replay workspace on an ephemeral loopback port, with an output root and service state in a
temporary directory, and prints one JSON line {"base": URL, "root": DIR} when it listens. After that every stdin line
is one JSON command and gets one JSON reply line ({"error": ...} on failure); the commands build replay bundles with
the real finalizer and change files the way a pipeline or a processing job would (scene graph files are published by
atomic rename, as the processing scripts do). Processing jobs run on a fake process runner instead of docker: the
commands job_emit / job_finish play the part of the container. Questions are answered by a scripted fake of the Agent
worker (agent_answer / agent_hold / agent_release / agent_fail / agent_key / agent_requests / agent_reset): no model, no
docker, and the API keys the service looks for are placeholders that agent_key can take away. End of input stops the service and deletes the fixtures. Diagnostics go to stderr only: stdout carries the protocol.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import cv2
import numpy as np

from scripts.replay_bundle import BundleOptions, finalize_bundle
from scripts.replay_web.jobs import JobManager
from scripts.replay_web.server import ServerConfig, create_server
from tests.replay_agent_fakes import ScriptedAgent
from tests.replay_fixtures import write_graph
from tests.test_replay_jobs import FakeRunner

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

	def write_graph(self, path: str, name: str, objects: Optional[List[int]] = None, rooms: Any = False, marker: str = "fixture") -> Dict[str, Any]:
		"""Publish a scene graph file (objects O<n>, optionally summarized rooms [[label, description], ...]) by atomic rename."""
		write_graph(Path(path) / name, marker, atomic=True, objects=objects if objects is not None else [7, 8], rooms=rooms)
		return {}

	def write_text(self, path: str, name: str, text: str) -> Dict[str, Any]:
		"""A file written in place, which a reader can see half done (an invalid graph, for example)."""
		(Path(path) / name).write_text(text, encoding="utf-8")
		return {}

	def remove_file(self, path: str, name: str) -> Dict[str, Any]:
		(Path(path) / name).unlink()
		return {}


class FakeJobs:
	"""The fake container of the processing jobs: the test decides what it prints, writes and exits with."""

	def __init__(self, root: Path):
		self.runner = FakeRunner()
		self.api_key = True
		self.manager = JobManager(state_root=root / "state", runner=self.runner, repo_root=REPO, api_key_check=lambda: self.api_key,
			busy_check=lambda: None, stop_container=self._stop_container, container_alive=lambda job_id: False, grace=0.3, stop_wait=0.3)

	def _stop_container(self, job_id: str) -> None:
		"""The fake `docker stop`: the container ends, and the docker client attached to it with it."""
		for call, process in zip(list(self.runner.calls), list(self.runner.processes)):
			if "daaam-replay-job-" + job_id in call["argv"]:
				process.finish(143)

	def calls(self) -> Dict[str, Any]:
		return {"calls": [call["argv"] for call in self.runner.calls]}

	def emit(self, line: str, index: int = -1) -> Dict[str, Any]:
		self.runner.processes[index].emit(line)
		return {}

	def finish(self, code: int = 0, files: Optional[List[str]] = None, path: Optional[str] = None, index: int = -1, rooms: Any = False) -> Dict[str, Any]:
		"""Write the files the stage publishes (scene graphs through the fixtures' writer), then exit with `code`."""
		for name in files or []:
			target = Path(path) / name
			if name.endswith(".json"):
				write_graph(target, name, atomic=True, objects=[7, 8], rooms=rooms if name.startswith("clustered_dsg_with") else False)
			else:
				target.write_text("region_summaries: []\n", encoding="utf-8")
		self.runner.processes[index].finish(code)
		return {}

	def set_api_key(self, present: bool) -> Dict[str, Any]:
		self.api_key = bool(present)
		return {}

	def reset(self) -> Dict[str, Any]:
		"""End every unfinished fake job and forget the commands run so far, so that a test starts with a free slot and
		sees only its own calls."""
		for process in self.runner.processes:
			process.finish(143)
		deadline = time.monotonic() + 10
		while time.monotonic() < deadline and any(job.state in ("queued", "running") for job in self.manager._jobs.values()):
			time.sleep(0.02)
		self.runner.calls.clear()
		self.runner.processes.clear()
		return {}


def main() -> int:
	root = Path(tempfile.mkdtemp(prefix="daaam-replay-browser-"))
	fixtures = Fixtures(root)
	jobs = FakeJobs(root)
	agent = ScriptedAgent()
	keys = {"OPENAI_API_KEY": "placeholder-for-the-fixture-service", "ANTHROPIC_API_KEY": "placeholder-for-the-fixture-service"}
	server = create_server(ServerConfig(repo_root=REPO, output_root=fixtures.output, state_root=root / "state",
		distro=os.environ.get("WSL_DISTRO_NAME", ""), port=0, graph_interval=0.2, agent_models=("gpt-5-mini", "claude-sonnet-4-5"),
		stage_root=root / "stage"), jobs=jobs.manager, agent=agent)
	server.service.chat.environ = keys   # the keys the chat sees are the test's, not this machine's environment or .env
	server.service.chat.repo_root = root   # (only their presence is ever looked at)

	def set_key(present: bool, variable: str = "OPENAI_API_KEY") -> Dict[str, Any]:
		if present:
			keys[variable] = "placeholder-for-the-fixture-service"
		else:
			keys.pop(variable, None)
		return {}

	def reset_agent() -> Dict[str, Any]:
		agent.reset()
		set_key(True)
		return set_key(True, "ANTHROPIC_API_KEY")
	thread = threading.Thread(target=server.serve_forever, daemon=True)
	thread.start()
	commands: Dict[str, Callable[..., Dict[str, Any]]] = {
		"make_result": fixtures.make_result,
		"remove_bundle": fixtures.remove_bundle,
		"exists": fixtures.exists,
		"corrupt_chunk": fixtures.corrupt_chunk,
		"write_graph": fixtures.write_graph,
		"write_text": fixtures.write_text,
		"remove_file": fixtures.remove_file,
		"job_calls": jobs.calls,
		"job_emit": jobs.emit,
		"job_finish": jobs.finish,
		"job_api_key": jobs.set_api_key,
		"job_reset": jobs.reset,
		"agent_requests": lambda: {"requests": agent.asked()},
		"agent_answer": lambda text: (setattr(agent, "answer", lambda request: text), {})[1],
		"agent_hold": lambda: (agent.hold(), {})[1],
		"agent_release": lambda: (agent.release(), {})[1],
		"agent_fail": lambda mode=None: (agent.fail(mode), {})[1],
		"agent_key": set_key,
		"agent_reset": reset_agent,
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
