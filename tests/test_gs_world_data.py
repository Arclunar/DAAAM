import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from scripts.gs_world import data
from scripts.gs_world.data import Camera, LayoutWriter, RGBDSequence, depth_to_png, filter_depth_edges, read_trajectory

K = np.array([[50.0, 0.0, 15.5], [0.0, 50.0, 11.5], [0.0, 0.0, 1.0]])


def pose(tx: float) -> np.ndarray:
	T = np.eye(4)
	T[0, 3] = tx
	return T


class CameraTest(unittest.TestCase):
	def test_scaling_keeps_the_pixel_centre_convention(self) -> None:
		half = Camera(32, 24, K).scaled(2)
		self.assertEqual((half.width, half.height), (16, 12))
		self.assertAlmostEqual(half.K[0, 0], 25.0)
		# centre of pixel 0 is at 0 in both images: cx' = (cx + 0.5) / 2 - 0.5
		self.assertAlmostEqual(half.K[0, 2], (15.5 + 0.5) / 2 - 0.5)
		self.assertAlmostEqual(half.K[1, 2], (11.5 + 0.5) / 2 - 0.5)
		self.assertIs(Camera(32, 24, K).scaled(1).K, K)

	def test_gsplat_intrinsics_shift_the_principal_point_by_half_a_pixel(self) -> None:
		shifted = Camera(32, 24, K).gsplat_K()
		self.assertAlmostEqual(shifted[0, 2], 16.0)
		self.assertAlmostEqual(shifted[1, 2], 12.0)
		self.assertAlmostEqual(K[0, 2], 15.5, msg="the camera's own K is unchanged")


class LayoutTest(unittest.TestCase):
	def setUp(self) -> None:
		self.tmp = tempfile.TemporaryDirectory()
		self.root = Path(self.tmp.name)

	def tearDown(self) -> None:
		self.tmp.cleanup()

	def write(self, out: Path, frames: int = 3, **kwargs) -> Path:
		writer = LayoutWriter(out, Camera(32, 24, K), fps=10.0, source="unit test", **kwargs)
		rng = np.random.default_rng(1)
		self.rgbs, self.depths = [], []
		for i in range(frames):
			rgb = rng.integers(0, 256, (24, 32, 3), dtype=np.uint8)
			depth = np.full((24, 32), 1.0 + 0.5 * i, np.float32)
			depth[0, 0] = 0.0  # no measurement
			depth[1, 1] = 20.0  # beyond the 16-bit range at 5000 units per metre
			writer.add(rgb, depth, pose(0.1 * i), 100.0 + 0.1 * i)
			self.rgbs.append(rgb)
			self.depths.append(depth)
		mask = np.ones((24, 32), bool)
		mask[:, :2] = False
		return writer.close(valid_mask=mask)

	def test_written_layout_is_read_back(self) -> None:
		out = self.write(self.root / "seq")
		self.assertFalse((self.root / "seq.partial").exists())
		info = json.loads((out / "camera_info.json").read_text())
		self.assertEqual(info["depth_scale"], 5000.0)
		self.assertEqual(info["distortion"], [0.0] * 5)
		self.assertEqual(info["source"], "unit test")
		seq = RGBDSequence(out)
		self.assertEqual(len(seq), 3)
		np.testing.assert_array_equal(seq.rgb(2), self.rgbs[2])
		depth = seq.depth(2)
		self.assertEqual(depth[0, 0], 0.0)
		self.assertEqual(depth[1, 1], 0.0, "unrepresentable depth becomes 'no measurement'")
		self.assertAlmostEqual(float(depth[5, 5]), 2.0, places=3)
		np.testing.assert_allclose(seq.poses[1], pose(0.1))
		np.testing.assert_allclose(seq.timestamps, [100.0, 100.1, 100.2])
		mask = seq.valid_mask()
		self.assertFalse(mask[:, :2].any())
		self.assertTrue(mask[:, 2:].all())
		self.assertEqual(seq.valid_mask(scale=2).shape, (12, 16))
		self.assertEqual(seq.rgb(0, scale=2).shape, (12, 16, 3))
		self.assertEqual(seq.depth(0, scale=2).shape, (12, 16))

	def test_existing_output_is_kept_unless_overwrite(self) -> None:
		out = self.write(self.root / "seq")
		with self.assertRaises(FileExistsError):
			LayoutWriter(out, Camera(32, 24, K), 10.0, "again")
		self.write(out, frames=2, overwrite=True)
		self.assertEqual(len(RGBDSequence(out)), 2)

	def test_failed_depth_write_does_not_publish_a_sequence_without_depth(self) -> None:
		out = self.root / "failed_depth"
		writer = LayoutWriter(out, Camera(32, 24, K), 10.0, "test")
		original_write = cv2.imwrite
		def write(path, image):
			return False if Path(path).parent.name == "depth" else original_write(path, image)
		with mock.patch.object(data.cv2, "imwrite", side_effect=write):
			with self.assertRaises(OSError):
				writer.add(np.zeros((24, 32, 3), np.uint8), np.ones((24, 32), np.float32), np.eye(4), 0.0)
		self.assertFalse(out.exists())
		self.assertEqual(writer.poses, [])

	def test_failed_valid_mask_write_does_not_publish_the_sequence(self) -> None:
		out = self.root / "failed_mask"
		writer = LayoutWriter(out, Camera(32, 24, K), 10.0, "test")
		writer.add(np.zeros((24, 32, 3), np.uint8), np.ones((24, 32), np.float32), np.eye(4), 0.0)
		with mock.patch.object(data.cv2, "imwrite", return_value=False):
			with self.assertRaises(OSError):
				writer.close(valid_mask=np.ones((24, 32), bool))
		self.assertFalse(out.exists())

	def test_depth_conversion(self) -> None:
		png = depth_to_png(np.array([[0.0, 1.0, np.nan, 13.1, 13.2, -1.0]], np.float32))
		self.assertEqual(png.dtype, np.uint16)
		self.assertEqual(png.tolist(), [[0, 5000, 0, 65500, 0, 0]])

	def test_sequences_with_distortion_or_missing_poses_are_refused(self) -> None:
		out = self.write(self.root / "seq")
		info = json.loads((out / "camera_info.json").read_text())
		info["distortion"] = [0.1, 0, 0, 0, 0]
		(out / "camera_info.json").write_text(json.dumps(info))
		with self.assertRaisesRegex(ValueError, "undistort"):
			RGBDSequence(out)
		info["distortion"] = [0.0] * 5
		(out / "camera_info.json").write_text(json.dumps(info))
		(out / "pose" / "poses.txt").write_text(" ".join(["1", "0", "0", "0", "0", "1", "0", "0", "0", "0", "1", "0", "0", "0", "0", "1"]) + "\n")
		with self.assertRaisesRegex(ValueError, "3 images but 1 poses"):
			RGBDSequence(out)

	def test_black_undistortion_border_is_found_without_a_mask_file(self) -> None:
		out = self.write(self.root / "seq", frames=4)
		(out / "valid_mask.png").unlink()
		for path in sorted((out / "rgb").iterdir()):
			image = cv2.imread(str(path))
			image[:3, :] = 0  # border band in every frame
			image[10, 10] = 0  # an isolated black pixel inside is image content
			cv2.imwrite(str(path), image)
		mask = RGBDSequence(out).valid_mask()
		self.assertFalse(mask[:3].any())
		self.assertFalse(mask[3].any(), "the region next to the border is shrunk by one pixel")
		self.assertTrue(mask[5:, 1:-1].all())

	def test_resolve_dataset_accepts_short_names(self) -> None:
		self.write(self.root / "tum_fr1_room")
		with mock.patch.object(data, "DATASETS_ROOT", self.root):
			self.assertEqual(data.resolve_dataset("fr1_room"), self.root / "tum_fr1_room")
			self.assertEqual(data.resolve_dataset("tum_fr1_room"), self.root / "tum_fr1_room")
			self.assertEqual(data.resolve_dataset(str(self.root / "tum_fr1_room")), self.root / "tum_fr1_room")
			with self.assertRaisesRegex(FileNotFoundError, "tum_fr1_room"):
				data.resolve_dataset("nothing")


class TrajectoryFileTest(unittest.TestCase):
	def test_matrix_tum_and_pose_only_formats(self) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "traj.txt"
			path.write_text("# header\n" + " ".join(f"{v:g}" for v in pose(1.5).reshape(-1)) + "\n")
			poses, times = read_trajectory(path)
			self.assertIsNone(times)
			np.testing.assert_allclose(poses[0], pose(1.5))
			path.write_text("10.0 1 2 3 0 0 0 1\n10.5 1 2 4 0 0 0.7071068 0.7071068\n")
			poses, times = read_trajectory(path)
			np.testing.assert_allclose(times, [10.0, 10.5])
			np.testing.assert_allclose(poses[1, :3, 3], [1, 2, 4])
			np.testing.assert_allclose(poses[1, :3, :3], [[0, -1, 0], [1, 0, 0], [0, 0, 1]], atol=1e-6)
			path.write_text("1 2 3 0 0 0 1\n")
			poses, times = read_trajectory(path)
			self.assertIsNone(times)
			path.write_text("1 2 3\n")
			with self.assertRaisesRegex(ValueError, "16, 8"):
				read_trajectory(path)


class DepthEdgeTest(unittest.TestCase):
	def test_pixels_at_a_depth_step_are_dropped(self) -> None:
		depth = np.full((10, 10), 1.0, np.float32)
		depth[:, 5:] = 2.0
		depth[0, 0] = 0.0
		filtered = filter_depth_edges(depth, 0.1)
		self.assertTrue(np.all(filtered[:, 4:6] == 0.0), "both sides of the step")
		self.assertTrue(np.all(filtered[:, 7:] == 2.0))
		self.assertTrue(np.all(filtered[1:, :3] == 1.0), "a missing measurement is not a discontinuity")


if __name__ == "__main__":
	unittest.main()
