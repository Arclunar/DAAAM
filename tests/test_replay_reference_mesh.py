import base64
import builtins
import contextlib
import gzip
import importlib.util
import io
import json
import re
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import numpy as np


REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("daaam_replay_reference", REPO / "scripts" / "replay.py")
REPLAY = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(REPLAY)
VIEWER = (REPO / "scripts" / "replay_viewer.html").read_text(encoding="utf-8")

DTYPES = {"f4": "<f4", "i4": "<i4", "u4": "<u4", "i2": "<i2", "u2": "<u2", "u1": "|u1"}


def write_snapshot(path: Path, frame: int, offset: float = 0.0) -> None:
	"""A minimal scene-graph snapshot (the arrays of replay.extract_snapshot) with a small tetrahedron as Hydra mesh."""
	np.savez(
		path / ("snap_%06d.npz" % frame),
		mesh_pos=np.array([[0, 0, 0], [2, 0, 0], [0, 2, 0], [0, 0, 2]], np.float32) + offset,
		mesh_col=np.array([[255, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 255]], np.uint8),
		mesh_faces=np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], np.uint32),
		obj_cid=np.array([7], np.int32), obj_label=np.array([42], np.uint32),
		obj_min=np.array([[offset, 0, 0]], np.float32), obj_max=np.array([[offset + 0.4, 0.4, 0.6]], np.float32),
		place_cid=np.array([3], np.int32), place_pos=np.array([[offset, 0, 0]], np.float32), place_rad=np.array([0.2], np.float32),
		place_edges=np.zeros((0, 2), np.int32), room_pos=np.zeros((0, 3), np.float32), room_place_edges=np.zeros((0, 2), np.int32),
		bld_pos=np.zeros((0, 3), np.float32), bld_room_edges=np.zeros((0, 2), np.int32), obj_place_edges=np.array([[0, 0]], np.int32),
		agent_pos=np.array([[offset, 0, 0]], np.float32), agent_quat=np.array([[1, 0, 0, 0]], np.float32),
		agent_t=np.array([frame / 15.0], np.float64), place_t=np.array([frame / 15.0], np.float64),
	)


def reference_arrays(side: int = 2, origin=(10.0, 20.0, 30.0)) -> dict:
	"""A flat side x side grid of vertices (two triangles per cell) far away from the Hydra mesh."""
	ys, xs = np.mgrid[0:side, 0:side]
	positions = np.stack([xs.ravel() * 0.01 + origin[0], ys.ravel() * 0.01 + origin[1], np.full(side * side, origin[2])], axis=1).astype(np.float32)
	index = np.arange(side * side).reshape(side, side)
	a, b, c, d = index[:-1, :-1].ravel(), index[:-1, 1:].ravel(), index[1:, :-1].ravel(), index[1:, 1:].ravel()
	return {
		"positions": positions,
		"colors": ((positions - positions.min(axis=0)) / np.maximum(np.ptp(positions, axis=0), 1e-6) * 255).astype(np.uint8),
		"normals": np.tile(np.array([0.0, 0.0, 1.0], np.float32), (side * side, 1)),
		"triangles": np.concatenate([np.stack([a, b, c], axis=1), np.stack([b, d, c], axis=1)]).astype(np.uint32),
	}


def extract_data(html: str) -> dict:
	start = html.index("window.REPLAY = ") + len("window.REPLAY = ")
	return json.loads(html[start: html.index(";</script>", start)].replace("<\\/", "</"))


def decode_chunk(chunk: str) -> dict:
	raw = zlib.decompress(base64.b64decode(chunk))
	head = struct.unpack_from("<I", raw, 0)[0]
	arrays = {}
	for entry in json.loads(raw[4: 4 + head]):
		count = int(np.prod(entry["s"]))
		arrays[entry["n"]] = np.frombuffer(raw, DTYPES[entry["t"]], count, 4 + head + entry["o"]).reshape(entry["s"])
	return arrays


class ReplayReferenceMeshTest(unittest.TestCase):
	def test_viewer_exposes_description_source_frame_in_object_details(self) -> None:
		template = (REPO / "scripts" / "replay_viewer.html").read_text(encoding="utf-8")
		self.assertIn("description_sources", template)
		self.assertIn("描述来源 frame", template)
		self.assertIn("entity-source", template)
		self.assertIn("openDescriptionSource", template)
		self.assertIn('id="description-source-modal"', template)

	def test_selecting_an_entity_does_not_move_the_timeline(self) -> None:
		template = (REPO / "scripts" / "replay_viewer.html").read_text(encoding="utf-8")
		body = template.split("function selectEntity", 1)[1].split("function assemble", 1)[0]
		self.assertNotIn("show(", body)

	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.root = Path(self.temp.name)
		self.recording = self.root / "recording"
		self.recording.mkdir()
		for frame in (1, 2):
			write_snapshot(self.recording, frame, offset=frame * 0.1)
		self.page = self.root / "replay.html"

	def tearDown(self) -> None:
		self.temp.cleanup()

	def save_reference(self, arrays=None, name: str = "reference.npz", **kwargs) -> Path:
		path = self.root / name
		np.savez(path, **(reference_arrays(**kwargs) if arrays is None else arrays))
		return path

	def build(self, *extra: str) -> subprocess.CompletedProcess:
		return subprocess.run(
			[sys.executable, str(REPO / "scripts" / "replay.py"), "build", str(self.recording), "--out", str(self.page), *extra],
			cwd=REPO, capture_output=True, text=True,
		)

	def test_a_page_without_a_reference_is_the_legacy_page(self) -> None:
		result = self.build()
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		data = extract_data(self.page.read_text(encoding="utf-8"))
		self.assertNotIn("reference_mesh", data)
		self.assertEqual(len(data["chunks"]), 2)
		self.assertNotIn("参考网格", result.stdout)

	def test_the_reference_is_embedded_with_its_own_quantization(self) -> None:
		reference = reference_arrays(side=40)
		result = self.build("--reference-mesh", str(self.save_reference(reference)))
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		data = extract_data(self.page.read_text(encoding="utf-8"))
		embedded = data["reference_mesh"]
		self.assertEqual((embedded["vertices"], embedded["triangles"]), (1600, 2 * 39 * 39))
		self.assertIn("1 cm", embedded["label"])
		self.assertNotEqual(embedded["quant"]["min"], data["quant"]["min"], "the reference needs a grid of its own, not the one of the Hydra snapshots")
		np.testing.assert_allclose(embedded["quant"]["min"], reference["positions"].min(axis=0), atol=1e-4)
		arrays = decode_chunk(embedded["chunk"])
		self.assertEqual(sorted(arrays), ["mesh_col", "mesh_faces", "mesh_nrm", "mesh_pos"])
		quant_min, quant_scale = np.array(embedded["quant"]["min"]), np.array(embedded["quant"]["scale"])
		restored = quant_min + (arrays["mesh_pos"].astype(np.float64) + 32768.0) * quant_scale
		np.testing.assert_allclose(restored, reference["positions"], atol=float(quant_scale.max()))
		np.testing.assert_array_equal(arrays["mesh_col"], reference["colors"])
		np.testing.assert_allclose(arrays["mesh_nrm"] / 32767.0, reference["normals"], atol=1e-4)
		np.testing.assert_array_equal(arrays["mesh_faces"], reference["triangles"])
		self.assertEqual(arrays["mesh_faces"].dtype, np.uint16)
		# the Hydra snapshots are untouched
		self.assertEqual(len(data["chunks"]), 2)
		self.assertEqual(decode_chunk(data["chunks"][0])["mesh_pos"].shape, (4, 3))

	def test_meshes_with_more_than_65535_vertices_keep_32_bit_indices(self) -> None:
		result = self.build("--reference-mesh", str(self.save_reference(side=300)))
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		arrays = decode_chunk(extract_data(self.page.read_text(encoding="utf-8"))["reference_mesh"]["chunk"])
		self.assertEqual(arrays["mesh_pos"].shape, (90000, 3))
		self.assertEqual(arrays["mesh_faces"].dtype, np.uint32)
		self.assertEqual(int(arrays["mesh_faces"].max()), 90000 - 1)

	def test_build_output_reports_counts_and_sizes(self) -> None:
		result = self.build("--reference-mesh", str(self.save_reference(side=40)))
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertRegex(result.stdout, r"reference mesh: 1,600 vertices, 3,042 triangles, payload \d+(\.\d+)? MB")
		self.assertRegex(result.stdout, r"\(\d+(\.\d+)? MB\)")

	def test_malformed_caches_are_rejected_with_the_path_and_no_page_is_written(self) -> None:
		good = reference_arrays()
		cases = {
			"missing": lambda a: a.pop("normals"),
			"nan": lambda a: a["positions"].__setitem__((0, 0), np.nan),
			"index": lambda a: a["triangles"].__setitem__((0, 0), 4),
			"shape": lambda a: a.update(colors=a["colors"][:, :2]),
			"empty": lambda a: a.update(triangles=np.zeros((0, 3), np.uint32)),
		}
		for name, damage in cases.items():
			with self.subTest(case=name):
				arrays = {key: value.copy() for key, value in good.items()}
				damage(arrays)
				path = self.save_reference(arrays, f"{name}.npz")
				result = self.build("--reference-mesh", str(path))
				self.assertNotEqual(result.returncode, 0, name)
				self.assertIn(path.name, result.stderr)
				self.assertFalse(self.page.exists(), "a rejected cache must not leave a page behind")

	def test_an_unreadable_cache_is_rejected(self) -> None:
		broken = self.root / "broken.npz"
		broken.write_bytes(b"this is not an npz archive")
		result = self.build("--reference-mesh", str(broken))
		self.assertNotEqual(result.returncode, 0)
		self.assertIn("broken.npz", result.stderr)
		missing = self.build("--reference-mesh", str(self.root / "missing.npz"))
		self.assertNotEqual(missing.returncode, 0)
		self.assertIn("missing.npz", missing.stderr)

	def test_building_from_a_cache_never_imports_open3d(self) -> None:
		real_import = builtins.__import__
		attempts = []

		def guarded(name, *args, **kwargs):
			if name.split(".")[0] == "open3d":
				attempts.append(name)
				raise ImportError("open3d must not be imported to embed a cache")
			return real_import(name, *args, **kwargs)

		args = REPLAY.argparse.Namespace(
			recording=str(self.recording), run=None, out=str(self.page), title=None, merge=0.005, dataset=None,
			thumb_width=320, max_depth=4.0, fps=15.0, reference_mesh=str(self.save_reference()),
		)
		with mock.patch.object(builtins, "__import__", guarded), contextlib.redirect_stdout(io.StringIO()):
			code = REPLAY.build(args)
		self.assertEqual(code, 0)
		self.assertEqual(attempts, [], "embedding a cache must not even try to import Open3D")
		self.assertNotIn("open3d", (REPO / "scripts" / "replay.py").read_text(encoding="utf-8"))

	def finalize(self, reference=None):
		from scripts.replay_bundle import BundleOptions, finalize_bundle

		run = self.root / "run"
		run.mkdir(exist_ok=True)
		return finalize_bundle(BundleOptions(recording=self.recording, source_graph=None, run_dir=run, output=run / "replay",
			dataset_kind="tum", sequence="fr1_desk", reference_mesh=reference))

	def test_the_bundle_stores_the_reference_once_with_its_own_quantization(self) -> None:
		reference = reference_arrays(side=40)
		manifest_path = self.finalize(self.save_reference(reference))
		manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
		entry = manifest["reference_mesh"]
		self.assertEqual((entry["vertices"], entry["triangles"]), (1600, 2 * 39 * 39))
		self.assertIn("1 cm", entry["label"])
		payload = json.loads(gzip.decompress((manifest_path.parent / entry["path"]).read_bytes()))
		self.assertNotEqual(payload["quant"]["min"], manifest["quant"]["min"], "the reference needs a grid of its own")
		arrays = decode_chunk(payload["chunk"])
		quant_min, quant_scale = np.array(payload["quant"]["min"]), np.array(payload["quant"]["scale"])
		restored = quant_min + (arrays["mesh_pos"].astype(np.float64) + 32768.0) * quant_scale
		np.testing.assert_allclose(restored, reference["positions"], atol=float(quant_scale.max()))
		np.testing.assert_allclose(arrays["mesh_nrm"] / 32767.0, reference["normals"], atol=1e-4)
		self.assertEqual(len(manifest["chunks"]), 1, "the Hydra snapshots are untouched by the reference")

	def test_a_bundle_without_a_reference_has_none(self) -> None:
		manifest = json.loads(self.finalize().read_text(encoding="utf-8"))
		self.assertIsNone(manifest["reference_mesh"])
		self.assertFalse((self.root / "run" / "replay" / "reference").exists())

	def test_a_malformed_cache_writes_no_bundle(self) -> None:
		arrays = reference_arrays()
		arrays.pop("normals")
		path = self.save_reference(arrays, "missing.npz")
		with self.assertRaises(ValueError) as caught:
			self.finalize(path)
		self.assertIn("missing.npz", str(caught.exception))
		self.assertFalse((self.root / "run" / "replay").exists())

	def test_finalizing_with_a_cache_never_imports_open3d(self) -> None:
		real_import = builtins.__import__
		attempts = []

		def guarded(name, *args, **kwargs):
			if name.split(".")[0] == "open3d":
				attempts.append(name)
				raise ImportError("open3d must not be imported to embed a cache")
			return real_import(name, *args, **kwargs)

		with mock.patch.object(builtins, "__import__", guarded):
			self.finalize(self.save_reference())
		self.assertEqual(attempts, [])
		self.assertNotIn("open3d", (REPO / "scripts" / "replay_bundle.py").read_text(encoding="utf-8"))

	def test_the_page_is_written_atomically(self) -> None:
		self.page.write_text("previous page")
		result = self.build("--reference-mesh", str(self.root / "missing.npz"))
		self.assertNotEqual(result.returncode, 0)
		self.assertEqual(self.page.read_text(), "previous page")
		self.assertEqual(self.build().returncode, 0)
		self.assertEqual(sorted(p.name for p in self.root.glob("replay.html*")), ["replay.html"])


class ReplayViewerTemplateTest(unittest.TestCase):
	def test_the_viewer_has_the_mesh_source_selector(self) -> None:
		self.assertIn('id="mesh-mode"', VIEWER)
		for option in ('value="reference"', 'value="dynamic"', 'value="none"'):
			self.assertIn(option, VIEWER)
		self.assertIn("1 cm 参考网格", VIEWER)
		self.assertIn("动态 Hydra 网格", VIEWER)
		self.assertIn("隐藏网格", VIEWER)
		self.assertIn('id="mesh-status"', VIEWER)
		self.assertIn("不是真值", VIEWER)

	def test_the_viewer_calls_the_static_mesh_a_reference_never_ground_truth(self) -> None:
		self.assertNotRegex(VIEWER, "真值网格|ground[- ]truth mesh")

	def test_the_viewer_is_self_contained(self) -> None:
		self.assertEqual(re.findall(r"https?://", VIEWER), [], "the page must not reference anything on the network")
		self.assertNotRegex(VIEWER, r"\b(fetch|XMLHttpRequest|WebSocket|importScripts)\s*\(")
		self.assertNotRegex(VIEWER, r"<script[^>]+src=|<link[^>]+href=")

	def test_the_viewer_has_the_label_mode_and_the_selection_overlay(self) -> None:
		self.assertIn('id="label-mode"', VIEWER)
		for option in ('value="selected"', 'value="all"', 'value="none"'):
			self.assertIn(option, VIEWER)
		self.assertNotIn('id="c-objlab"', VIEWER)
		self.assertIn('id="selection-overlay"', VIEWER)
		self.assertRegex(VIEWER, r"#selection-overlay\s*\{[^}]*pointer-events:\s*none")   # the overlay never takes a click from the canvas
		self.assertIn("#ffb000", VIEWER)

	def test_label_membership_no_longer_depends_on_the_camera(self) -> None:
		for removed in ("textWidth", "taken.some", "shown.length >= 80"):
			self.assertNotIn(removed, VIEWER)


if __name__ == "__main__":
	unittest.main()
