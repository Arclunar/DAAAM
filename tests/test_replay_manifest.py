import gzip
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.replay_web.manifest import (
	GRAPH_CANDIDATES,
	SCHEMA_VERSION,
	ManifestError,
	resolve_bundle_member,
	validate_manifest,
	write_json_atomic,
)


def write_chunk(path: Path, records) -> dict:
	body = json.dumps({"snapshots": records}, separators=(",", ":")).encode("utf-8")
	payload = gzip.compress(body, mtime=0)
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_bytes(payload)
	return {"bytes": len(payload), "uncompressed_bytes": len(body), "sha256": hashlib.sha256(payload).hexdigest()}


class ReplayManifestTest(unittest.TestCase):
	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.base = Path(self.temp.name).resolve()
		self.bundles = 0

	def tearDown(self) -> None:
		self.temp.cleanup()

	def make_bundle(self, dataset_kind: str = "tum", **overrides) -> Path:
		"""A fresh minimal finalized bundle: two chunks, an RGB/depth pair per snapshot and one description image."""
		self.bundles += 1
		root = self.base / ("result%d" % self.bundles) / "replay"
		first = write_chunk(root / "timeline/000000.json.gz", [{"n": 1, "t": 0.067, "p": ""}, {"n": 5, "t": 0.333, "p": ""}])
		second = write_chunk(root / "timeline/000001.json.gz", [{"n": 9, "t": 0.6, "p": ""}])
		for name in ("rgb/000000.jpg", "depth/000000.jpg", "rgb/000004.jpg", "depth/000004.jpg", "rgb/000008.jpg", "depth/000008.jpg"):
			(root / "images/timeline" / name).parent.mkdir(parents=True, exist_ok=True)
			(root / "images/timeline" / name).write_bytes(b"\xff\xd8\xff\xd9")
		(root / "images/descriptions").mkdir(parents=True)
		(root / "images/descriptions/000001.jpg").write_bytes(b"\xff\xd8\xff\xd9")
		manifest = {
			"schema_version": SCHEMA_VERSION,
			"generator": {"name": "test", "version": 1},
			"created_at": "2026-10-17T00:00:00Z",
			"finalized": True,
			"title": "fixture",
			"dataset": {
				"kind": dataset_kind,
				"sequence": "fr1_desk" if dataset_kind == "tum" else "0",
				"time_origin": 0.0 if dataset_kind == "tum" else 1673884185.589118,
				"fps": 15.0 if dataset_kind == "tum" else 10.0,
				"start_frame": 0,
				"frame_count": None,
				"source": None,
			},
			"timebase": {"unit": "s", "first": 0.067, "last": 0.6},
			"coordinates": {"frame": "world", "up": "z"},
			"quant": {"min": [0.0, 0.0, 0.0], "scale": [1.0, 1.0, 1.0]},
			"snapshots": {"count": 3, "numbers": [1, 5, 9], "times": [0.067, 0.333, 0.6]},
			"chunks": [
				{"index": 0, "path": "timeline/000000.json.gz", "first_snapshot": 0, "snapshot_count": 2, "t_first": 0.067, "t_last": 0.333, **first},
				{"index": 1, "path": "timeline/000001.json.gz", "first_snapshot": 2, "snapshot_count": 1, "t_first": 0.6, "t_last": 0.6, **second},
			],
			"layers": ["mesh", "objects", "places", "rooms", "buildings", "agent"],
			"cameras": [{"key": "rgb", "label": "RGB"}, {"key": "depth", "label": "深度", "max_depth": 4.0}],
			"images": {
				"frame_total": 9,
				"timeline": [
					{"index": 0, "rgb": "images/timeline/rgb/000000.jpg", "depth": "images/timeline/depth/000000.jpg"},
					{"index": 4, "rgb": "images/timeline/rgb/000004.jpg", "depth": "images/timeline/depth/000004.jpg"},
					{"index": 8, "rgb": "images/timeline/rgb/000008.jpg", "depth": "images/timeline/depth/000008.jpg"},
				],
				"descriptions": {
					"2": {"frame_id": 2, "timestamp": 0.125, "camera": "rgb", "index": 1, "path": "images/descriptions/000001.jpg"},
				},
			},
			"labels": {"42": "a green chair"},
			"description_sources": {"42": {"frame_id": 2, "timestamp": 0.125}},
			"catalog": {"objects": [], "places": []},
			"reference_mesh": None,
			"graph_candidates": list(GRAPH_CANDIDATES),
			"build": {},
		}
		for key, value in overrides.items():
			manifest[key] = value
		(root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
		return root

	def edit(self, root: Path, change) -> None:
		manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
		change(manifest)
		(root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

	def assertRejected(self, root: Path, fragment: str) -> None:
		with self.assertRaises(ManifestError) as caught:
			validate_manifest(root)
		self.assertIn(fragment, str(caught.exception))

	def test_valid_manifest_resolves_only_members_inside_the_bundle(self) -> None:
		root = self.make_bundle(dataset_kind="tum")
		manifest = validate_manifest(root)
		self.assertEqual(manifest["schema_version"], 1)
		self.assertEqual(manifest["dataset"]["kind"], "tum")
		self.assertEqual(resolve_bundle_member(root, "timeline/000000.json.gz"), root / "timeline/000000.json.gz")

	def test_absolute_parent_and_escaping_symlink_members_are_rejected(self) -> None:
		root = self.make_bundle(dataset_kind="coda")
		(self.base / "outside").write_text("secret", encoding="utf-8")
		(root / "images" / "escape.jpg").symlink_to(self.base / "outside")
		for value in ("/etc/passwd", "../outside", "timeline/../../outside", "images/escape.jpg"):
			with self.subTest(value=value), self.assertRaises(ManifestError):
				resolve_bundle_member(root, value)

	def test_non_normalized_member_names_are_rejected(self) -> None:
		root = self.make_bundle()
		for value in ("", "./timeline/000000.json.gz", "timeline//000000.json.gz", "timeline\\000000.json.gz", "timeline/000000.json.gz/", "C:/x", "timeline/\x00"):
			with self.subTest(value=value), self.assertRaises(ManifestError):
				resolve_bundle_member(root, value)
		with self.assertRaises(ManifestError):
			resolve_bundle_member(root, 42)

	def test_a_symlink_inside_the_bundle_that_stays_inside_is_allowed(self) -> None:
		root = self.make_bundle()
		(root / "alias.json.gz").symlink_to(root / "timeline/000000.json.gz")
		self.assertEqual(resolve_bundle_member(root, "alias.json.gz"), root / "timeline/000000.json.gz")

	def test_missing_manifest_names_the_expected_path_and_schema(self) -> None:
		empty = self.base / "empty" / "replay"
		empty.mkdir(parents=True)
		with self.assertRaises(ManifestError) as caught:
			validate_manifest(empty)
		self.assertIn(str(empty / "manifest.json"), str(caught.exception))
		self.assertIn("schema 1", str(caught.exception))

	def test_malformed_json_is_rejected(self) -> None:
		root = self.make_bundle()
		(root / "manifest.json").write_text("{", encoding="utf-8")
		self.assertRejected(root, "not valid JSON")
		(root / "manifest.json").write_text("[]", encoding="utf-8")
		self.assertRejected(root, "JSON object")

	def test_unknown_schema_version_is_rejected(self) -> None:
		for version in (2, 0, "1", True):
			with self.subTest(version=version):
				root = self.make_bundle()
				self.edit(root, lambda m: m.__setitem__("schema_version", version))
				self.assertRejected(root, "schema_version")

	def test_an_unfinalized_bundle_is_not_openable(self) -> None:
		root = self.make_bundle(finalized=False)
		self.assertRejected(root, "finalized")

	def test_dataset_kind_must_be_tum_or_coda(self) -> None:
		root = self.make_bundle()
		self.edit(root, lambda m: m["dataset"].__setitem__("kind", "kitti"))
		self.assertRejected(root, "dataset.kind")

	def test_required_keys_are_never_filled_in_silently(self) -> None:
		for key in ("schema_version", "finalized", "dataset", "timebase", "snapshots", "chunks", "cameras", "images", "graph_candidates"):
			with self.subTest(key=key):
				root = self.make_bundle()
				self.edit(root, lambda m: m.pop(key))
				self.assertRejected(root, key)

	def test_dataset_metadata_types_are_checked(self) -> None:
		cases = {
			"sequence": "",
			"time_origin": "0",
			"fps": 0,
			"start_frame": -1,
			"frame_count": 0,
		}
		for field, value in cases.items():
			with self.subTest(field=field):
				root = self.make_bundle()
				self.edit(root, lambda m: m["dataset"].__setitem__(field, value))
				self.assertRejected(root, "dataset." + field)

	def test_a_missing_chunk_is_rejected(self) -> None:
		root = self.make_bundle()
		(root / "timeline/000001.json.gz").unlink()
		self.assertRejected(root, "timeline/000001.json.gz")

	def test_a_bad_sha256_string_is_rejected(self) -> None:
		root = self.make_bundle()
		self.edit(root, lambda m: m["chunks"][0].__setitem__("sha256", "not-a-digest"))
		self.assertRejected(root, "sha256")

	def test_a_chunk_whose_symlink_leaves_the_bundle_is_rejected(self) -> None:
		root = self.make_bundle()
		outside = self.base / "elsewhere.json.gz"
		(root / "timeline/000001.json.gz").rename(outside)
		(root / "timeline/000001.json.gz").symlink_to(outside)
		self.assertRejected(root, "leaves")

	def test_chunk_size_must_match_the_file(self) -> None:
		root = self.make_bundle()
		self.edit(root, lambda m: m["chunks"][1].__setitem__("bytes", m["chunks"][1]["bytes"] + 1))
		self.assertRejected(root, "bytes")

	def test_digests_are_verified_on_request(self) -> None:
		root = self.make_bundle()
		validate_manifest(root, verify_digests=True)
		path = root / "timeline/000000.json.gz"
		data = bytearray(path.read_bytes())
		data[-1] ^= 0xFF
		path.write_bytes(bytes(data))
		validate_manifest(root)   # size and containment are unchanged: the cheap check passes
		with self.assertRaises(ManifestError) as caught:
			validate_manifest(root, verify_digests=True)
		self.assertIn("timeline/000000.json.gz", str(caught.exception))

	def test_chunks_must_cover_the_snapshots_contiguously(self) -> None:
		root = self.make_bundle()
		self.edit(root, lambda m: m["chunks"][1].__setitem__("first_snapshot", 3))
		self.assertRejected(root, "first_snapshot")
		root = self.make_bundle()
		self.edit(root, lambda m: m["snapshots"].__setitem__("count", 4))
		self.assertRejected(root, "snapshots")

	def test_snapshot_index_lengths_and_order_are_checked(self) -> None:
		root = self.make_bundle()
		self.edit(root, lambda m: m["snapshots"].__setitem__("times", [0.067, 0.6, 0.333]))
		self.assertRejected(root, "snapshots.times")
		root = self.make_bundle()
		self.edit(root, lambda m: m["snapshots"].__setitem__("numbers", [1, 5]))
		self.assertRejected(root, "snapshots.numbers")

	def test_image_members_are_validated(self) -> None:
		root = self.make_bundle()
		(root / "images/descriptions/000001.jpg").unlink()
		self.assertRejected(root, "images/descriptions/000001.jpg")
		root = self.make_bundle()
		self.edit(root, lambda m: m["images"]["timeline"][0].__setitem__("rgb", "../../etc/passwd"))
		self.assertRejected(root, "unsafe")
		root = self.make_bundle()
		self.edit(root, lambda m: m["images"]["timeline"].pop())
		self.assertRejected(root, "images.timeline")

	def test_timeline_images_may_be_absent_for_one_snapshot(self) -> None:
		root = self.make_bundle()
		self.edit(root, lambda m: m["images"]["timeline"][1].update(rgb=None, depth=None))
		self.assertIsNone(validate_manifest(root)["images"]["timeline"][1]["rgb"])

	def test_a_bundle_without_cameras_has_no_timeline_images(self) -> None:
		root = self.make_bundle(cameras=[])
		self.edit(root, lambda m: m["images"].update(timeline=[], frame_total=0))
		self.assertEqual(validate_manifest(root)["cameras"], [])

	def test_graph_candidates_are_plain_known_file_names(self) -> None:
		root = self.make_bundle(graph_candidates=["../dsg.json"])
		self.assertRejected(root, "graph_candidates")

	def test_the_reference_mesh_member_is_validated(self) -> None:
		root = self.make_bundle(reference_mesh={"path": "reference/reference.json.gz", "vertices": 4, "triangles": 2, "label": "1 cm"})
		self.assertRejected(root, "reference/reference.json.gz")
		(root / "reference").mkdir()
		(root / "reference/reference.json.gz").write_bytes(gzip.compress(b"{}", mtime=0))
		self.assertEqual(validate_manifest(root)["reference_mesh"]["vertices"], 4)


class WriteJsonAtomicTest(unittest.TestCase):
	def test_the_target_is_replaced_whole_and_no_temporary_file_remains(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			target = Path(temp) / "manifest.json"
			target.write_text('{"old": true}', encoding="utf-8")
			write_json_atomic(target, {"new": "数据"})
			self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"new": "数据"})
			self.assertEqual(sorted(os.listdir(temp)), ["manifest.json"])

	def test_a_failed_write_keeps_the_old_file_and_removes_the_temporary_file(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			target = Path(temp) / "state.json"
			target.write_text('{"old": true}', encoding="utf-8")
			with self.assertRaises(TypeError):
				write_json_atomic(target, {"bad": object()})
			self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"old": True})
			self.assertEqual(sorted(os.listdir(temp)), ["state.json"])


if __name__ == "__main__":
	unittest.main()
