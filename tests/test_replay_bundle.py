import base64
import gzip
import json
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from scripts import replay_bundle
from scripts.replay_bundle import BundleOptions, encode_chunk, finalize_bundle
from scripts.replay_web.manifest import ManifestError, validate_manifest

try:
	import spark_dsg
except ImportError:   # the host has no spark_dsg; the container does (just test runs this module there)
	spark_dsg = None


REPO = Path(__file__).resolve().parents[1]
DTYPES = {"f4": "<f4", "i4": "<i4", "u4": "<u4", "i2": "<i2", "u2": "<u2", "u1": "|u1"}
CODA_ORIGIN = 1673884185.589118


def write_snapshot(path: Path, frame: int, offset: float = 0.0, fps: float = 15.0, origin: float = 0.0, vertices: int = 4) -> None:
	"""One compact scene-graph snapshot (the arrays of replay.extract_snapshot)."""
	base = np.array([[0, 0, 0], [2, 0, 0], [0, 2, 0], [0, 0, 2]], np.float32)
	extra = np.random.RandomState(frame).uniform(0, 2, (max(0, vertices - 4), 3)).astype(np.float32)
	positions = np.concatenate([base, extra]) + offset
	np.savez(
		path / ("snap_%06d.npz" % frame),
		mesh_pos=positions,
		mesh_col=np.full((len(positions), 3), 200, np.uint8),
		mesh_faces=np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], np.uint32),
		obj_cid=np.array([7], np.int32), obj_label=np.array([42], np.uint32),
		obj_min=np.array([[offset, 0, 0]], np.float32), obj_max=np.array([[offset + 0.4, 0.4, 0.6]], np.float32),
		place_cid=np.array([3], np.int32), place_pos=np.array([[offset, 0, 0]], np.float32), place_rad=np.array([0.2], np.float32),
		place_edges=np.zeros((0, 2), np.int32), room_pos=np.zeros((0, 3), np.float32), room_place_edges=np.zeros((0, 2), np.int32),
		bld_pos=np.zeros((0, 3), np.float32), bld_room_edges=np.zeros((0, 2), np.int32), obj_place_edges=np.array([[0, 0]], np.int32),
		agent_pos=np.array([[offset, 0, 0]], np.float32), agent_quat=np.array([[1, 0, 0, 0]], np.float32),
		agent_t=np.array([origin + frame / fps], np.float64), place_t=np.array([origin + frame / fps], np.float64),
	)


def write_reference(path: Path, side: int = 3) -> Path:
	ys, xs = np.mgrid[0:side, 0:side]
	positions = np.stack([xs.ravel() * 0.01 + 10, ys.ravel() * 0.01 + 20, np.full(side * side, 30.0)], axis=1).astype(np.float32)
	index = np.arange(side * side).reshape(side, side)
	a, b, c, d = index[:-1, :-1].ravel(), index[:-1, 1:].ravel(), index[1:, :-1].ravel(), index[1:, 1:].ravel()
	np.savez(
		path,
		positions=positions,
		colors=np.full((side * side, 3), 90, np.uint8),
		normals=np.tile(np.array([0.0, 0.0, 1.0], np.float32), (side * side, 1)),
		triangles=np.concatenate([np.stack([a, b, c], axis=1), np.stack([b, d, c], axis=1)]).astype(np.uint32),
	)
	return path


def colour(index: int):
	return (min(255, index * 20), max(0, 255 - index * 20), 100)


def read_chunk(path: Path) -> list:
	return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))["snapshots"]


def unpack(record: dict) -> dict:
	raw = zlib.decompress(base64.b64decode(record["p"]))
	head = struct.unpack_from("<I", raw, 0)[0]
	arrays = {}
	for entry in json.loads(raw[4: 4 + head]):
		count = int(np.prod(entry["s"]))
		arrays[entry["n"]] = np.frombuffer(raw, DTYPES[entry["t"]], count, 4 + head + entry["o"]).reshape(entry["s"])
	return arrays


def mean_bgr(path: Path):
	image = cv2.imread(str(path))
	return [float(image[:, :, k].mean()) for k in range(3)]


class FinalizeTumBundleTest(unittest.TestCase):
	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.root = Path(self.temp.name)
		self.recording = self.root / "recording"
		self.recording.mkdir()
		for frame, offset in ((1, 0.0), (5, 0.3), (9, 0.6)):
			write_snapshot(self.recording, frame, offset)
		self.run = self.root / "output" / "tum_replay" / "out_20261017_000000"
		self.run.mkdir(parents=True)
		(self.run / "corrections.yaml").write_text(
			"label_names:\n  - label: 42\n    name: a green chair\n"
			"    description_source:\n      frame_id: 2\n      timestamp: 0.133\n",
			encoding="utf-8",
		)
		self.dataset = self.root / "tum_fr1_desk"
		(self.dataset / "rgb").mkdir(parents=True)
		(self.dataset / "depth").mkdir()
		for index in range(2):
			cv2.imwrite(str(self.dataset / "rgb" / ("%06d.png" % index)), np.full((12, 16, 3), colour(index * 5), np.uint8))
			cv2.imwrite(str(self.dataset / "depth" / ("%06d.png" % index)), np.full((12, 16), 5000 * (index + 1), np.uint16))
		(self.dataset / "camera_info.json").write_text(json.dumps({"depth_scale": 5000.0}), encoding="utf-8")

	def tearDown(self) -> None:
		self.temp.cleanup()

	def options(self, **overrides) -> BundleOptions:
		values = dict(
			recording=self.recording, source_graph=None, run_dir=self.run, output=self.run / "replay",
			dataset_kind="tum", sequence="fr1_desk", dataset=self.dataset, fps=15.0, thumb_width=8,
		)
		values.update(overrides)
		return BundleOptions(**values)

	def test_finalize_writes_manifest_last_and_external_image_assets(self) -> None:
		options = self.options(max_chunk_snapshots=2)
		seen = []
		real = replay_bundle.write_json_atomic

		def spy(path, value):
			folder = Path(path).parent
			seen.append((Path(path).name, sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())))
			return real(path, value)

		with mock.patch.object(replay_bundle, "write_json_atomic", spy):
			manifest_path = finalize_bundle(options)
		self.assertEqual(manifest_path, options.output / "manifest.json")
		self.assertEqual([name for name, _ in seen], ["manifest.json"], "the manifest is the only JSON committed separately")
		before_manifest = seen[0][1]
		self.assertIn("timeline/000000.json.gz", before_manifest)
		self.assertIn("timeline/000001.json.gz", before_manifest)
		self.assertNotIn("manifest.json", before_manifest)

		manifest = json.loads(manifest_path.read_text())
		self.assertEqual(manifest["dataset"]["kind"], "tum")
		self.assertEqual(manifest["dataset"]["sequence"], "fr1_desk")
		self.assertEqual(manifest["dataset"]["time_origin"], 0.0)
		self.assertEqual([c["snapshot_count"] for c in manifest["chunks"]], [2, 1])
		self.assertEqual([c["first_snapshot"] for c in manifest["chunks"]], [0, 2])
		self.assertFalse(any("data:image" in p.read_text(errors="ignore") for p in options.output.rglob("*.json")))
		self.assertTrue((options.output / manifest["images"]["timeline"][0]["rgb"]).is_file())
		self.assertTrue((options.output / manifest["images"]["timeline"][0]["depth"]).is_file())
		self.assertEqual(validate_manifest(options.output, verify_digests=True)["snapshots"]["count"], 3)
		self.assertEqual(manifest["snapshots"]["numbers"], [1, 5, 9])
		self.assertEqual(manifest["snapshots"]["times"], [round(1 / 15, 3), round(5 / 15, 3), round(9 / 15, 3)])
		self.assertEqual(sorted(p.name for p in self.run.iterdir()), ["corrections.yaml", "replay"], "no temporary directory remains")

	def test_chunk_records_carry_the_packed_snapshot_arrays(self) -> None:
		manifest = json.loads(finalize_bundle(self.options(max_chunk_snapshots=2)).read_text())
		records = read_chunk(self.run / "replay" / manifest["chunks"][0]["path"]) + read_chunk(self.run / "replay" / manifest["chunks"][1]["path"])
		self.assertEqual([r["n"] for r in records], [1, 5, 9])
		arrays = unpack(records[1])
		self.assertNotIn("place_t", arrays)
		self.assertEqual(arrays["mesh_faces"].dtype, np.uint16)
		quant = manifest["quant"]
		restored = np.array(quant["min"]) + (arrays["mesh_pos"].astype(np.float64) + 32768.0) * np.array(quant["scale"])
		np.testing.assert_allclose(restored[0], [0.3, 0.3, 0.3], atol=float(max(quant["scale"])) * 2)
		chunk = manifest["chunks"][0]
		self.assertEqual(chunk["path"], "timeline/000000.json.gz")
		self.assertEqual((chunk["t_first"], chunk["t_last"]), (round(1 / 15, 3), round(5 / 15, 3)))
		raw = (self.run / "replay" / chunk["path"]).read_bytes()
		self.assertEqual(chunk["bytes"], len(raw))
		self.assertEqual(chunk["uncompressed_bytes"], len(gzip.decompress(raw)))

	def test_the_byte_limit_separates_large_snapshots_and_still_allows_an_oversized_one(self) -> None:
		for frame, offset in ((1, 0.0), (5, 0.3), (9, 0.6)):
			write_snapshot(self.recording, frame, offset, vertices=400)
		single = json.loads(finalize_bundle(self.options(max_chunk_snapshots=1)).read_text())
		largest = max(chunk["uncompressed_bytes"] for chunk in single["chunks"])
		roomy = json.loads(finalize_bundle(self.options(max_chunk_snapshots=24, max_chunk_bytes=largest * 10)).read_text())
		self.assertEqual([c["snapshot_count"] for c in roomy["chunks"]], [3])
		limit = int(largest * 1.5)
		tight = json.loads(finalize_bundle(self.options(max_chunk_snapshots=24, max_chunk_bytes=limit)).read_text())
		self.assertEqual([c["snapshot_count"] for c in tight["chunks"]], [1, 1, 1], "two payloads together exceed the byte limit")
		self.assertTrue(all(c["uncompressed_bytes"] <= limit for c in tight["chunks"]))
		self.assertEqual(encode_chunk([{"n": 1}]), encode_chunk([{"n": 1}]), "chunks are deterministic (no gzip time stamp)")
		small = json.loads(finalize_bundle(self.options(max_chunk_snapshots=24, max_chunk_bytes=10)).read_text())
		self.assertEqual([c["snapshot_count"] for c in small["chunks"]], [1, 1, 1], "a snapshot larger than the limit gets a chunk of its own")
		self.assertGreater(small["chunks"][0]["uncompressed_bytes"], 10)

	def test_description_sources_are_exact_dataset_frames_shared_per_frame(self) -> None:
		manifest = json.loads(finalize_bundle(self.options()).read_text())
		self.assertEqual(manifest["labels"], {"42": "a green chair"})
		self.assertEqual(manifest["description_sources"], {"42": {"frame_id": 2, "timestamp": 0.133}})
		source = manifest["images"]["descriptions"]["2"]
		self.assertEqual((source["frame_id"], source["index"], source["camera"]), (2, 1, "rgb"))
		self.assertAlmostEqual(source["timestamp"], 0.133)
		blue, green, red = mean_bgr(self.run / "replay" / source["path"])
		self.assertGreater(green, 120, "frame ID 2 is the second dataset frame")
		self.assertEqual(manifest["images"]["frame_total"], 2)
		self.assertEqual([entry["index"] for entry in manifest["images"]["timeline"]], [1, 1, 1], "the run has two frames")
		self.assertEqual([c["key"] for c in manifest["cameras"]], ["rgb", "depth"])
		self.assertEqual(manifest["cameras"][1]["max_depth"], 4.0)

	def test_a_missing_source_frame_stays_unavailable(self) -> None:
		(self.run / "corrections.yaml").write_text(
			"label_names:\n  - label: 42\n    name: a green chair\n"
			"    description_source:\n      frame_id: 99\n      timestamp: 6.6\n",
			encoding="utf-8",
		)
		manifest = json.loads(finalize_bundle(self.options()).read_text())
		self.assertEqual(manifest["images"]["descriptions"], {})
		self.assertIn("42", manifest["description_sources"], "the recorded provenance is kept, only its image is missing")

	def test_without_a_dataset_there_are_no_cameras(self) -> None:
		manifest = json.loads(finalize_bundle(self.options(dataset=None)).read_text())
		self.assertEqual(manifest["cameras"], [])
		self.assertEqual(manifest["images"]["timeline"], [])
		self.assertEqual(validate_manifest(self.run / "replay")["snapshots"]["count"], 3)

	def test_catalog_title_and_build_parameters_are_recorded(self) -> None:
		manifest = json.loads(finalize_bundle(self.options(title="tum_replay / out_x", merge=0.0)).read_text())
		self.assertEqual(manifest["title"], "tum_replay / out_x")
		self.assertEqual([item["id"] for item in manifest["catalog"]["objects"]], [7])
		self.assertEqual([item["id"] for item in manifest["catalog"]["places"]], [3])
		self.assertEqual(manifest["build"]["merge"], 0.0)
		self.assertEqual(manifest["build"]["recording"], str(self.recording))
		self.assertIsNone(manifest["build"]["source_graph"])
		self.assertEqual(manifest["graph_candidates"], ["clustered_dsg_with_summaries.json", "clustered_dsg.json", "dsg_updated.json", "dsg.json"])
		self.assertEqual((manifest["timebase"]["first"], manifest["timebase"]["last"]), (round(1 / 15, 3), round(9 / 15, 3)))
		self.assertIs(manifest["finalized"], True)
		self.assertEqual(manifest["schema_version"], 1)

	def test_the_reference_mesh_is_a_separate_member_with_its_own_grid(self) -> None:
		reference = write_reference(self.root / "reference.npz")
		manifest = json.loads(finalize_bundle(self.options(reference_mesh=reference)).read_text())
		entry = manifest["reference_mesh"]
		self.assertEqual(entry["path"], "reference/reference.json.gz")
		self.assertEqual((entry["vertices"], entry["triangles"]), (9, 8))
		self.assertIn("1 cm", entry["label"])
		payload = json.loads(gzip.decompress((self.run / "replay" / entry["path"]).read_bytes()))
		self.assertEqual(payload["vertices"], 9)
		self.assertNotEqual(payload["quant"]["min"], manifest["quant"]["min"])
		self.assertIn("chunk", payload)

	def test_a_failed_finalization_keeps_the_previous_bundle(self) -> None:
		finalize_bundle(self.options(title="first"))
		broken = self.root / "broken.npz"
		broken.write_bytes(b"not an archive")
		with self.assertRaises(ValueError):
			finalize_bundle(self.options(title="second", reference_mesh=broken))
		self.assertEqual(json.loads((self.run / "replay" / "manifest.json").read_text())["title"], "first")
		self.assertEqual(sorted(p.name for p in self.run.iterdir()), ["corrections.yaml", "replay"])

	def test_publishing_again_replaces_the_bundle_whole(self) -> None:
		finalize_bundle(self.options(title="first", max_chunk_snapshots=1))
		finalize_bundle(self.options(title="second", max_chunk_snapshots=24))
		manifest = validate_manifest(self.run / "replay", verify_digests=True)
		self.assertEqual(manifest["title"], "second")
		self.assertEqual(sorted(p.name for p in (self.run / "replay" / "timeline").iterdir()), ["000000.json.gz"])
		self.assertEqual(sorted(p.name for p in self.run.iterdir()), ["corrections.yaml", "replay"])

	def test_a_failed_publication_restores_the_previous_bundle(self) -> None:
		finalize_bundle(self.options(title="first"))
		real_replace = replay_bundle.os.replace

		def failing(source, target):   # publishing the new bundle fails; moving the old one back still works
			if Path(source).name.endswith(".tmp") and Path(target).name == "replay":
				raise OSError("simulated rename failure")
			return real_replace(source, target)

		with mock.patch.object(replay_bundle.os, "replace", failing):
			with self.assertRaises(OSError):
				finalize_bundle(self.options(title="second"))
		self.assertEqual(json.loads((self.run / "replay" / "manifest.json").read_text())["title"], "first")
		self.assertEqual(sorted(p.name for p in self.run.iterdir()), ["corrections.yaml", "replay"])

	def test_invalid_inputs_are_rejected_before_anything_is_written(self) -> None:
		empty = self.root / "empty"
		empty.mkdir()
		cases = {
			"no snapshots": dict(recording=empty),
			"unsupported dataset kind": dict(dataset_kind="kitti"),
			"exactly one": dict(source_graph=self.run / "dsg.json"),
			"exactly one of": dict(recording=None),
			"frame_count": dict(recording=None, source_graph=self.run / "dsg.json"),
		}
		for fragment, overrides in cases.items():
			with self.subTest(fragment=fragment):
				with self.assertRaises(ValueError) as caught:
					finalize_bundle(self.options(**overrides))
				self.assertIn(fragment, str(caught.exception))
		self.assertFalse((self.run / "replay").exists())


class FinalizeCodaBundleTest(unittest.TestCase):
	"""CODa: unpadded file numbers, the sequence time origin, the run's start frame and a static final-graph bundle."""

	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.root = Path(self.temp.name)
		self.coda = self.root / "CODa"
		rgb = self.coda / "2d_rect" / "cam0" / "0"
		depth = self.coda / "3d_raw_estimated" / "cam0" / "0"
		rgb.mkdir(parents=True)
		depth.mkdir(parents=True)
		for index in range(12):   # names sort as 0, 1, 10, 11, 2, ...: only numeric order is right
			cv2.imwrite(str(rgb / ("2d_rect_cam0_0_%d.png" % index)), np.full((12, 16, 3), colour(index), np.uint8))
			cv2.imwrite(str(depth / ("depth_cam0_0_%d.png" % index)), np.full((12, 16), 1000 * index, np.uint16))
		self.stamps = [CODA_ORIGIN + 0.1 * index for index in range(12)]
		(self.coda / "timestamps").mkdir()
		(self.coda / "timestamps" / "0.txt").write_text("".join("%.6f\n" % t for t in self.stamps), encoding="utf-8")
		self.run = self.root / "output" / "coda" / "out_20261017_000000"
		self.run.mkdir(parents=True)
		(self.run / "corrections.yaml").write_text(
			"label_names:\n  - label: 42\n    name: a bench\n"
			"    description_source:\n      frame_id: 3\n      timestamp: %.6f\n" % self.stamps[10],
			encoding="utf-8",
		)

	def tearDown(self) -> None:
		self.temp.cleanup()

	def test_a_recording_uses_numeric_frames_the_time_origin_and_the_start_frame(self) -> None:
		recording = self.root / "recording"
		recording.mkdir()
		for frame in (1, 2, 3):   # absolute stamps of dataset frames 8, 9, 10
			write_snapshot(recording, frame, fps=10.0, origin=CODA_ORIGIN + 0.7)
		options = BundleOptions(
			recording=recording, source_graph=None, run_dir=self.run, output=self.run / "replay", dataset_kind="coda",
			sequence="0", dataset=self.coda, fps=10.0, start_frame=8, frame_count=3, thumb_width=8,
		)
		manifest = json.loads(finalize_bundle(options).read_text())
		self.assertAlmostEqual(manifest["dataset"]["time_origin"], CODA_ORIGIN, places=6)
		self.assertEqual(manifest["dataset"]["start_frame"], 8)
		self.assertEqual(manifest["snapshots"]["times"], [0.8, 0.9, 1.0], "times are relative to the sequence start")
		self.assertEqual([entry["index"] for entry in manifest["images"]["timeline"]], [8, 9, 10])
		self.assertEqual(manifest["images"]["frame_total"], 12)
		last = mean_bgr(self.run / "replay" / manifest["images"]["timeline"][2]["rgb"])
		self.assertAlmostEqual(last[0], colour(10)[0], delta=12)
		source = manifest["images"]["descriptions"]["3"]
		self.assertEqual((source["index"], source["camera"]), (10, "cam0"))
		self.assertAlmostEqual(mean_bgr(self.run / "replay" / source["path"])[0], colour(10)[0], delta=12)
		self.assertEqual(manifest["cameras"][1]["max_depth"], 30.0)

	def test_an_explicit_time_origin_must_agree_with_the_dataset(self) -> None:
		recording = self.root / "recording"
		recording.mkdir()
		write_snapshot(recording, 1, fps=10.0, origin=CODA_ORIGIN)
		options = BundleOptions(
			recording=recording, source_graph=None, run_dir=self.run, output=self.run / "replay", dataset_kind="coda",
			sequence="0", dataset=self.coda, fps=10.0, time_origin=CODA_ORIGIN + 3.0, start_frame=0, frame_count=3,
		)
		with self.assertRaises(ValueError) as caught:
			finalize_bundle(options)
		self.assertIn("time_origin", str(caught.exception))

	@unittest.skipIf(spark_dsg is None, "spark_dsg is only installed in the daaam-ws container")
	def test_a_final_graph_without_a_recording_becomes_a_one_snapshot_bundle(self) -> None:
		graph = spark_dsg.DynamicSceneGraph()
		attributes = spark_dsg.ObjectNodeAttributes()
		attributes.position = np.array([1.0, 2.0, 0.5])
		attributes.semantic_label = 42
		graph.add_node(spark_dsg.DsgLayers.OBJECTS, spark_dsg.NodeSymbol("O", 7), attributes)
		graph.save(str(self.run / "dsg.json"))
		options = BundleOptions(
			recording=None, source_graph=self.run / "dsg.json", run_dir=self.run, output=self.run / "replay",
			dataset_kind="coda", sequence="0", dataset=self.coda, fps=10.0, time_origin=CODA_ORIGIN,
			start_frame=8, frame_count=3, thumb_width=8,
		)
		manifest = validate_manifest(finalize_bundle(options).parent, verify_digests=True)
		self.assertEqual(manifest["snapshots"]["count"], 1)
		self.assertEqual(manifest["snapshots"]["numbers"], [11], "snapshot number start_frame + frame_count")
		self.assertEqual(manifest["snapshots"]["times"], [1.0], "the last frame of the run, relative to the time origin")
		self.assertEqual([entry["index"] for entry in manifest["images"]["timeline"]], [10])
		self.assertEqual(manifest["build"]["source_graph"], str(self.run / "dsg.json"))
		self.assertEqual([item["id"] for item in manifest["catalog"]["objects"]], [7])
		arrays = unpack(read_chunk(self.run / "replay" / manifest["chunks"][0]["path"])[0])
		self.assertEqual(arrays["obj_cid"].tolist(), [7])


class FinalizeCommandTest(unittest.TestCase):
	def test_the_command_finalizes_a_recording_and_rejects_two_sources(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			recording, run = root / "recording", root / "run"
			recording.mkdir()
			run.mkdir()
			write_snapshot(recording, 1)
			base = [sys.executable, str(REPO / "scripts" / "replay.py"), "finalize"]
			ok = subprocess.run(
				base + [str(recording), "--run", str(run), "--dataset-kind", "tum", "--sequence", "fr1_desk", "--out", str(run / "replay")],
				cwd=REPO, capture_output=True, text=True,
			)
			self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
			self.assertIn("replay/manifest.json", ok.stdout)
			validate_manifest(run / "replay")
			both = subprocess.run(
				base + [str(recording), "--graph", str(run / "dsg.json"), "--run", str(run), "--dataset-kind", "tum", "--sequence", "x", "--out", str(run / "replay")],
				cwd=REPO, capture_output=True, text=True,
			)
			self.assertEqual(both.returncode, 2, both.stdout + both.stderr)
			self.assertIn("exactly one", both.stderr)
			neither = subprocess.run(base + ["--run", str(run), "--dataset-kind", "coda", "--sequence", "0", "--out", str(run / "replay")], cwd=REPO, capture_output=True, text=True)
			self.assertEqual(neither.returncode, 2, neither.stdout + neither.stderr)


if __name__ == "__main__":
	unittest.main()
