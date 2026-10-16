import importlib.util
import base64
import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("daaam_replay", REPO / "scripts" / "replay.py")
REPLAY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPLAY)


def snapshot(
	object_ids=(),
	object_labels=(),
	object_min=(),
	object_max=(),
	place_ids=(),
	place_pos=(),
	place_radius=(),
):
	return {
		"obj_cid": np.asarray(object_ids, np.int32),
		"obj_label": np.asarray(object_labels, np.uint32),
		"obj_min": np.asarray(object_min, np.float32).reshape(-1, 3),
		"obj_max": np.asarray(object_max, np.float32).reshape(-1, 3),
		"place_cid": np.asarray(place_ids, np.int32),
		"place_pos": np.asarray(place_pos, np.float32).reshape(-1, 3),
		"place_rad": np.asarray(place_radius, np.float32),
	}


class ReplayCatalogTest(unittest.TestCase):
	def test_description_sources_load_metadata_and_exact_one_based_dataset_frame(self) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp)
			run = root / "run"
			dataset = root / "dataset"
			(run).mkdir()
			(dataset / "rgb").mkdir(parents=True)
			(run / "corrections.yaml").write_text(
				"label_names:\n"
				"  - label: 42\n"
				"    name: a green chair\n"
				"    description_source:\n"
				"      frame_id: 2\n"
				"      timestamp: 0.125\n",
				encoding="utf-8",
			)
			cv2.imwrite(str(dataset / "rgb" / "000000.png"), np.full((8, 8, 3), (0, 0, 255), np.uint8))
			cv2.imwrite(str(dataset / "rgb" / "000001.png"), np.full((8, 8, 3), (0, 255, 0), np.uint8))

			sources = REPLAY.description_sources(run)
			images = REPLAY.description_source_images(
				dataset,
				{**sources, "43": {"frame_id": 2, "timestamp": 0.125}},
				8,
			)

			self.assertEqual(sources, {"42": {"frame_id": 2, "timestamp": 0.125}})
			self.assertEqual(list(images), ["2"], "objects described from one frame must share one embedded JPEG")
			encoded = base64.b64decode(images["2"])
			decoded = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_COLOR)
			self.assertGreater(float(decoded[:, :, 1].mean()), 240.0)
			self.assertLess(float(decoded[:, :, 2].mean()), 15.0)

	def test_description_source_image_is_optional_for_old_or_missing_data(self) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp)
			root.mkdir(exist_ok=True)
			self.assertEqual(REPLAY.description_sources(root), {})
			self.assertEqual(
				REPLAY.description_source_images(root, {"42": {"frame_id": 99, "timestamp": 1.0}}, 8),
				{},
			)

	def test_catalog_summarizes_entity_lifetime_and_object_motion(self) -> None:
		snapshots = [
			snapshot(
				object_ids=[7],
				object_labels=[42],
				object_min=[[0.0, 0.0, 0.0]],
				object_max=[[2.0, 2.0, 2.0]],
				place_ids=[3],
				place_pos=[[0.0, 0.0, 0.0]],
				place_radius=[0.5],
			),
			snapshot(
				object_ids=[7, 8],
				object_labels=[42, 99],
				object_min=[[3.0, 0.0, 0.0], [9.0, 0.0, 0.0]],
				object_max=[[5.0, 2.0, 2.0], [11.0, 2.0, 2.0]],
				place_ids=[3, 4],
				place_pos=[[0.5, 0.0, 0.0], [8.0, 0.0, 0.0]],
				place_radius=[0.6, 0.8],
			),
		]

		catalog = REPLAY.entity_catalog(snapshots, [1.25, 3.75])

		self.assertEqual(
			catalog["objects"],
			[
				{"id": 7, "label": 42, "first": 1.25, "last": 3.75, "count": 2, "snapshots": [0, 1], "motion": 3.0},
				{"id": 8, "label": 99, "first": 3.75, "last": 3.75, "count": 1, "snapshots": [1], "motion": 0.0},
			],
		)
		self.assertEqual(
			catalog["places"],
			[
				{"id": 3, "first": 1.25, "last": 3.75, "count": 2, "snapshots": [0, 1]},
				{"id": 4, "first": 3.75, "last": 3.75, "count": 1, "snapshots": [1]},
			],
		)

	def test_missing_place_ids_are_filled_for_older_recordings(self) -> None:
		snap = snapshot(place_pos=[[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], place_radius=[0.2, 0.3])
		del snap["place_cid"]

		REPLAY.ensure_entity_ids([snap])

		np.testing.assert_array_equal(snap["place_cid"], np.array([0, 1], np.int32))

	def test_the_bundle_records_the_catalog_and_one_exact_source_image_per_frame(self) -> None:
		from scripts.replay_bundle import BundleOptions, finalize_bundle

		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp)
			recording, run, dataset = root / "recording", root / "run", root / "dataset"
			recording.mkdir()
			run.mkdir()
			(dataset / "rgb").mkdir(parents=True)
			cv2.imwrite(str(dataset / "rgb" / "000000.png"), np.full((8, 8, 3), (0, 0, 255), np.uint8))
			cv2.imwrite(str(dataset / "rgb" / "000001.png"), np.full((8, 8, 3), (0, 255, 0), np.uint8))
			arrays = {key: np.zeros((0, 3), np.float32) for key in ("mesh_pos", "room_pos", "bld_pos", "agent_pos")}
			arrays.update(snapshot(object_ids=[7, 8], object_labels=[42, 43], object_min=[[0, 0, 0], [1, 0, 0]], object_max=[[1, 1, 1], [2, 1, 1]],
				place_ids=[3], place_pos=[[0, 0, 0]], place_radius=[0.5]))
			arrays.update(mesh_col=np.zeros((0, 3), np.uint8), mesh_faces=np.zeros((0, 3), np.uint32), place_edges=np.zeros((0, 2), np.int32),
				room_place_edges=np.zeros((0, 2), np.int32), bld_room_edges=np.zeros((0, 2), np.int32), obj_place_edges=np.zeros((0, 2), np.int32),
				agent_quat=np.zeros((0, 4), np.float32), place_t=np.array([0.2]))
			np.savez(recording / "snap_000003.npz", **arrays)
			(run / "corrections.yaml").write_text(
				"label_names:\n"
				"  - label: 42\n    name: a green chair\n    description_source:\n      frame_id: 2\n      timestamp: 0.125\n"
				"  - label: 43\n    name: a green table\n    description_source:\n      frame_id: 2\n      timestamp: 0.125\n",
				encoding="utf-8",
			)
			bundle = run / "replay"
			finalize_bundle(BundleOptions(recording=recording, source_graph=None, run_dir=run, output=bundle, dataset_kind="tum",
				sequence="fr1_desk", dataset=dataset, thumb_width=8))
			manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))

			self.assertEqual(manifest["catalog"], REPLAY.entity_catalog([snapshot(object_ids=[7, 8], object_labels=[42, 43],
				object_min=[[0, 0, 0], [1, 0, 0]], object_max=[[1, 1, 1], [2, 1, 1]], place_ids=[3], place_pos=[[0, 0, 0]], place_radius=[0.5])], [0.2]))
			self.assertEqual(manifest["description_sources"], {"42": {"frame_id": 2, "timestamp": 0.125}, "43": {"frame_id": 2, "timestamp": 0.125}})
			self.assertEqual(list(manifest["images"]["descriptions"]), ["2"], "objects described from one frame share one image")
			image = cv2.imread(str(bundle / manifest["images"]["descriptions"]["2"]["path"]))
			self.assertGreater(float(image[:, :, 1].mean()), 240.0)
			self.assertEqual(sorted(p.name for p in (bundle / "images" / "descriptions").iterdir()), ["000001.jpg"])

	def test_legacy_place_ids_survive_reorder_insertion_and_deletion(self) -> None:
		snapshots = [
			snapshot(place_pos=[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]], place_radius=[0.2, 0.2]),
			snapshot(place_pos=[[1.01, 0.0, 0.0], [2.0, 0.0, 0.0], [0.01, 0.0, 0.0]], place_radius=[0.2, 0.2, 0.2]),
			snapshot(place_pos=[[2.01, 0.0, 0.0], [0.02, 0.0, 0.0]], place_radius=[0.2, 0.2]),
		]
		for snap in snapshots:
			del snap["place_cid"]

		REPLAY.ensure_entity_ids(snapshots)

		np.testing.assert_array_equal(snapshots[0]["place_cid"], np.array([0, 1], np.int32))
		np.testing.assert_array_equal(snapshots[1]["place_cid"], np.array([1, 2, 0], np.int32))
		np.testing.assert_array_equal(snapshots[2]["place_cid"], np.array([2, 0], np.int32))


if __name__ == "__main__":
	unittest.main()
