import json
import tempfile
import unittest
from pathlib import Path

from scripts.replay_datasets import CodaDatasetAssets, TumDatasetAssets, dataset_assets


CODA_ORIGIN = 1673884185.589118


def touch(path: Path) -> Path:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_bytes(b"")
	return path


class TumDatasetAssetsTest(unittest.TestCase):
	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.root = Path(self.temp.name) / "tum_fr1_desk"
		for index in range(4):
			touch(self.root / "rgb" / ("%06d.png" % index))
			touch(self.root / "depth" / ("%06d.png" % index))
		(self.root / "camera_info.json").write_text(json.dumps({"depth_scale": 5000.0}), encoding="utf-8")

	def tearDown(self) -> None:
		self.temp.cleanup()

	def test_frames_depth_and_scale_follow_the_tum_layout(self) -> None:
		assets = TumDatasetAssets(self.root, fps=15.0)
		self.assertEqual(assets.kind, "tum")
		self.assertEqual(assets.frame_total, 4)
		self.assertEqual(assets.run_range(), (0, 4))
		self.assertEqual(assets.rgb_path(2), self.root / "rgb" / "000002.png")
		self.assertEqual(assets.depth_path(2), self.root / "depth" / "000002.png")
		self.assertEqual(assets.depth_scale, 5000.0)
		self.assertIsNone(assets.dataset_time_origin())

	def test_snapshot_time_maps_to_the_processed_frame_clamped_into_the_run(self) -> None:
		assets = TumDatasetAssets(self.root, fps=15.0, frame_count=3)
		self.assertEqual(assets.timeline_index(1 / 15.0), 1)
		self.assertEqual(assets.timeline_index(-0.5), 0)
		self.assertEqual(assets.timeline_index(99.0), 2, "the declared run ends at its third frame")

	def test_description_frames_are_one_based_and_never_guessed(self) -> None:
		assets = TumDatasetAssets(self.root, fps=15.0)
		self.assertEqual(assets.description_index({"frame_id": 2, "timestamp": 0.133}), 1)
		for source in ({"frame_id": 0}, {"frame_id": 5}, {"frame_id": "x"}, {}):
			with self.subTest(source=source):
				self.assertIsNone(assets.description_index(source))

	def test_a_missing_depth_image_is_reported_as_absent(self) -> None:
		(self.root / "depth" / "000003.png").unlink()
		self.assertIsNone(TumDatasetAssets(self.root, fps=15.0).depth_path(3))


class CodaDatasetAssetsTest(unittest.TestCase):
	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.root = Path(self.temp.name) / "CODa"
		# lexicographically 1, 10, 11, 2, ... : the adapter must order by the number
		for index in range(12):
			touch(self.root / "2d_rect" / "cam0" / "0" / ("2d_rect_cam0_0_%d.png" % index))
			touch(self.root / "3d_raw_estimated" / "cam0" / "0" / ("depth_cam0_0_%d.png" % index))
		self.stamps = [CODA_ORIGIN + 0.1 * index + (0.0003 if index % 2 else 0.0) for index in range(12)]
		(self.root / "timestamps").mkdir()
		(self.root / "timestamps" / "0.txt").write_text("".join("%.6f\n" % t for t in self.stamps), encoding="utf-8")

	def tearDown(self) -> None:
		self.temp.cleanup()

	def test_frames_are_ordered_numerically_and_the_time_origin_is_the_first_timestamp(self) -> None:
		assets = CodaDatasetAssets(self.root, sequence="0", fps=10.0)
		self.assertEqual(assets.kind, "coda")
		self.assertEqual(assets.frame_total, 12)
		self.assertEqual(assets.rgb_path(2).name, "2d_rect_cam0_0_2.png")
		self.assertEqual(assets.rgb_path(10).name, "2d_rect_cam0_0_10.png")
		self.assertEqual(assets.depth_path(10).name, "depth_cam0_0_10.png")
		self.assertEqual(assets.depth_scale, 1000.0)
		self.assertAlmostEqual(assets.dataset_time_origin(), CODA_ORIGIN, places=6)

	def test_the_run_range_and_timeline_lookup_use_the_start_frame(self) -> None:
		assets = CodaDatasetAssets(self.root, sequence="0", fps=10.0, start_frame=4, frame_count=5)
		self.assertEqual(assets.run_range(), (4, 9))
		self.assertEqual(assets.timeline_index(0.8), 8)
		self.assertEqual(assets.timeline_index(0.0), 4, "times before the run clamp to its first frame")
		self.assertEqual(assets.timeline_index(1.1), 8)

	def test_a_run_relative_description_frame_is_offset_by_the_start_frame(self) -> None:
		assets = CodaDatasetAssets(self.root, sequence="0", fps=10.0, start_frame=4, frame_count=7)
		self.assertEqual(assets.description_index({"frame_id": 3, "timestamp": self.stamps[6]}), 6)
		self.assertEqual(assets.description_index({"frame_id": 3}), 6, "without a stamp the frame ID alone decides")

	def test_the_recorded_stamp_wins_over_a_frame_id_shifted_by_dropped_frames(self) -> None:
		assets = CodaDatasetAssets(self.root, sequence="0", fps=10.0, start_frame=4, frame_count=7)
		# the node dropped one frame before this one: frame ID 3 is really the dataset frame with this stamp
		self.assertEqual(assets.description_index({"frame_id": 3, "timestamp": self.stamps[7]}), 7)
		self.assertIsNone(assets.description_index({"frame_id": 3, "timestamp": CODA_ORIGIN + 55.0}), "no frame has that stamp")
		self.assertIsNone(assets.description_index({"frame_id": 3, "timestamp": self.stamps[11]}), "outside the run")

	def test_a_missing_sequence_folder_has_no_frames(self) -> None:
		assets = CodaDatasetAssets(self.root, sequence="3", fps=10.0)
		self.assertEqual(assets.frame_total, 0)
		self.assertIsNone(assets.timeline_index(0.0))
		self.assertIsNone(assets.dataset_time_origin())


class DatasetSelectionTest(unittest.TestCase):
	def test_the_kind_is_explicit_never_inferred_from_the_folder_name(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp) / "coda_looking_name"
			touch(root / "rgb" / "000000.png")
			self.assertIsInstance(dataset_assets("tum", root, sequence="fr1_desk", fps=15.0), TumDatasetAssets)
			self.assertIsInstance(dataset_assets("coda", root, sequence="0", fps=10.0), CodaDatasetAssets)
			with self.assertRaises(ValueError):
				dataset_assets("kitti", root, sequence="0", fps=10.0)


if __name__ == "__main__":
	unittest.main()
