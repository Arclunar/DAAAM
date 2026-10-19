import tempfile
import unittest
from pathlib import Path

import numpy as np
import torch

from scripts.gs_world.compare_maps import compare, jaccard, match_objects
from scripts.gs_world.data import Camera, LayoutWriter, RGBDSequence
from scripts.gs_world.geometry import (interpolate_poses, invert, matrix_to_quat, offset_in_camera_frame, pose_matrix,
	quat_to_matrix, rotation_angle_deg, scene_extent)
from scripts.gs_world.model import (SH_C0, backproject, create_params, initial_points, load_checkpoint, read_ply,
	save_checkpoint, sh_degree_of, voxel_downsample, write_ply)
from scripts.gs_world.rosbag_to_rgbd import select_keyframes
from scripts.gs_world.train import split_frames

K = np.array([[20.0, 0.0, 7.5], [0.0, 20.0, 5.5], [0.0, 0.0, 1.0]])


def yaw(deg: float, t=(0.0, 0.0, 0.0)) -> np.ndarray:
	a = np.radians(deg)
	T = np.eye(4)
	T[:3, :3] = [[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]]
	T[:3, 3] = t
	return T


class GeometryTest(unittest.TestCase):
	def test_quaternion_round_trip(self) -> None:
		for q in ([0, 0, 0, 1], [0.1, -0.7, 0.2, 0.67], [1, 0, 0, 0], [0, 0.6, 0.8, 0]):
			q = np.asarray(q, float) / np.linalg.norm(q)
			back = matrix_to_quat(quat_to_matrix(q))
			self.assertTrue(np.allclose(back, q, atol=1e-9) or np.allclose(back, -q, atol=1e-9))

	def test_interpolation_between_bracketing_poses(self) -> None:
		times = np.array([0.0, 1.0, 3.0])
		poses = np.array([yaw(0, (0, 0, 0)), yaw(90, (1, 0, 0)), yaw(90, (2, 0, 0))])
		out, ok = interpolate_poses(times, poses, np.array([0.5, 0.0, 1.0, 2.0, -0.1, 3.0, 3.1]), max_gap=1.5)
		self.assertEqual(ok.tolist(), [True, True, True, False, False, True, False])
		np.testing.assert_allclose(out[0, :3, 3], [0.5, 0, 0])
		self.assertAlmostEqual(rotation_angle_deg(out[0, :3, :3]), 45.0, places=6)
		np.testing.assert_allclose(out[5], poses[2])

	def test_offset_is_expressed_in_the_camera_frame(self) -> None:
		# optical camera looking along world +x (z_cam = x_world), with y_cam = -z_world (down)
		T = np.eye(4)
		T[:3, :3] = np.column_stack([[0, -1, 0], [0, 0, -1], [1, 0, 0]])
		moved = offset_in_camera_frame(T[None], (0.0, -0.2, 1.0))[0]
		np.testing.assert_allclose(moved[:3, 3], [1.0, 0.0, 0.2], atol=1e-12)  # 1 m forward, 20 cm up
		turned = offset_in_camera_frame(T[None], rotation_deg=(0.0, 90.0, 0.0))[0]
		np.testing.assert_allclose(turned[:3, 2], [0.0, -1.0, 0.0], atol=1e-12)  # now looking to the camera's right

	def test_inverse_and_scene_extent(self) -> None:
		T = yaw(30, (1, 2, 3))
		np.testing.assert_allclose(invert(T) @ T, np.eye(4), atol=1e-12)
		poses = np.array([yaw(0, (-1, 0, 0)), yaw(0, (1, 0, 0))])
		self.assertAlmostEqual(scene_extent(poses), 1.1)
		self.assertEqual(scene_extent(poses[:1]), 0.5, "a minimum keeps the learning rate of the positions sensible")


class InitialisationTest(unittest.TestCase):
	def test_backprojection_of_a_fronto_parallel_plane(self) -> None:
		depth = np.full((12, 16), 2.0, np.float32)
		depth[0, :] = 0.0
		rgb = np.zeros((12, 16, 3), np.uint8)
		rgb[..., 1] = 255
		points, colors = backproject(depth, Camera(16, 12, K), pose_matrix((0, 0, 1), (0, 0, 0, 1)), 1, rgb)
		self.assertEqual(len(points), 11 * 16)
		np.testing.assert_allclose(points[:, 2], 3.0)
		u, v = 15, 11
		self.assertTrue(np.any(np.all(np.isclose(points, [(u - 7.5) / 20 * 2, (v - 5.5) / 20 * 2, 3.0], atol=1e-6), axis=1)))
		np.testing.assert_allclose(colors[0], [0, 1, 0])

	def test_voxel_downsampling_averages_points_and_colours(self) -> None:
		points = np.array([[0.001, 0.002, 0.003], [0.009, 0.008, 0.007], [0.5, 0.5, 0.5]], np.float32)
		colors = np.array([[1, 0, 0], [0, 0, 1], [0, 1, 0]], np.float32)
		out_points, out_colors = voxel_downsample(points, colors, 0.01)
		self.assertEqual(len(out_points), 2)
		row = int(np.argmin(out_points[:, 0]))
		np.testing.assert_allclose(out_points[row], [0.005, 0.005, 0.005], atol=1e-6)
		np.testing.assert_allclose(out_colors[row], [0.5, 0, 0.5])

	def test_initial_points_fill_pixels_without_depth(self) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			writer = LayoutWriter(Path(tmp) / "seq", Camera(16, 12, K), 10.0, "wall")
			for i in range(3):
				depth = np.full((12, 16), 2.0, np.float32)
				depth[:, :4] = 0.0  # e.g. a window the sensor cannot measure
				writer.add(np.full((12, 16, 3), 128, np.uint8), depth, yaw(0, (0.01 * i, 0, 0)), float(i))
			seq = RGBDSequence(writer.close())
			points, _ = initial_points(seq, range(3), voxel=0.001, max_depth=6.0, edge_jump=0.0)
			self.assertTrue(np.any(np.isclose(points[:, 2], 2.0)))
			self.assertTrue(np.any(np.isclose(points[:, 2], 6.0)), "missing depth gets points at the maximum depth")
			plain, _ = initial_points(seq, range(3), voxel=0.001, max_depth=6.0, edge_jump=0.0, fill_missing=False)
			self.assertFalse(np.any(np.isclose(plain[:, 2], 6.0)))

	def test_parameters_start_as_isotropic_gaussians_with_the_point_colours(self) -> None:
		points = np.random.default_rng(0).uniform(-1, 1, (50, 3)).astype(np.float32)
		colors = np.tile(np.array([[0.25, 0.5, 1.0]], np.float32), (50, 1))
		params = create_params(points, colors, sh_degree=2, init_opacity=0.5, device="cpu")
		self.assertEqual(tuple(params["shN"].shape), (50, 8, 3))
		self.assertEqual(sh_degree_of(params), 2)
		np.testing.assert_allclose(params["sh0"][0, 0].detach().numpy(), (colors[0] - 0.5) / SH_C0, rtol=1e-6)
		np.testing.assert_allclose(params["quats"][:, 0].detach().numpy(), 1.0)
		self.assertTrue(torch.allclose(params["opacities"], torch.zeros(50), atol=1e-6))
		scales = params["scales"].detach().numpy()
		np.testing.assert_allclose(scales[:, 0], scales[:, 2])


class ExportTest(unittest.TestCase):
	def test_ply_matches_the_original_3dgs_layout(self) -> None:
		n = 4
		params = {
			"means": torch.arange(n * 3, dtype=torch.float32).reshape(n, 3),
			"scales": torch.full((n, 3), -2.0),
			"quats": torch.tensor([[2.0, 0, 0, 0]] * n),
			"opacities": torch.full((n,), 0.3),
			"sh0": torch.full((n, 1, 3), 0.1),
			"shN": torch.arange(n * 3 * 3, dtype=torch.float32).reshape(n, 3, 3),  # degree 1: 3 coefficients x RGB
		}
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "point_cloud.ply"
			self.assertEqual(write_ply(path, params), n)
			header = path.read_bytes().split(b"end_header")[0].decode()
			self.assertIn("format binary_little_endian 1.0", header)
			fields = read_ply(path)
		self.assertEqual(list(fields)[:9], ["x", "y", "z", "nx", "ny", "nz", "f_dc_0", "f_dc_1", "f_dc_2"])
		self.assertEqual(list(fields)[-8:], ["opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"])
		np.testing.assert_allclose(fields["y"], [1, 4, 7, 10])
		np.testing.assert_allclose(fields["rot_0"], 1.0, err_msg="quaternions are normalised")
		np.testing.assert_allclose(fields["opacity"], 0.3)
		# f_rest is channel-major: all red coefficients first (shN[0, :, 0] = 0, 3, 6)
		np.testing.assert_allclose([fields[f"f_rest_{i}"][0] for i in range(9)], [0, 3, 6, 1, 4, 7, 2, 5, 8])

	def test_checkpoint_round_trip(self) -> None:
		params = create_params(np.zeros((5, 3), np.float32) + np.arange(5)[:, None], np.full((5, 3), 0.5, np.float32),
			sh_degree=1, device="cpu")
		with tempfile.TemporaryDirectory() as tmp:
			save_checkpoint(Path(tmp) / "model.pt", params, {"steps": 7})
			loaded, meta = load_checkpoint(Path(tmp) / "model.pt", device="cpu")
		self.assertEqual(meta["steps"], 7)
		self.assertTrue(torch.equal(loaded["means"], params["means"].detach()))


class FrameSelectionTest(unittest.TestCase):
	def test_every_eighth_frame_is_held_out(self) -> None:
		train, test = split_frames(20, 8)
		self.assertEqual(test, [0, 8, 16])
		self.assertEqual(len(train) + len(test), 20)
		self.assertEqual(split_frames(10, 8), (list(range(10)), []), "too short to hold frames out")
		self.assertEqual(split_frames(10, 0)[1], [])

	def test_keyframes_take_the_sharpest_image_of_each_stretch(self) -> None:
		poses = np.array([yaw(0, (0.01 * i, 0, 0)) for i in range(20)])  # 1 cm per frame
		scores = [1.0] * 20
		scores[3] = 5.0
		scores[7] = 9.0
		keep = select_keyframes(poses, scores, min_translation=0.05, min_rotation_deg=10.0)
		self.assertEqual(keep[0], 3, "the first stretch is frames 0-4")
		self.assertIn(7, keep)
		gaps = np.diff([poses[k][0, 3] for k in keep])
		self.assertTrue(np.all(gaps >= 0.025 - 1e-9), "no near-duplicate keyframes")
		self.assertTrue(np.all(gaps <= 0.1 + 1e-9))
		turning = np.array([yaw(3 * i) for i in range(10)])  # standing still, turning 3 degrees per frame
		self.assertGreaterEqual(len(select_keyframes(turning, [1.0] * 10, 0.05, 5.0)), 4)


class MapComparisonTest(unittest.TestCase):
	def test_objects_are_matched_one_to_one_closest_first(self) -> None:
		reference = np.array([[0.0, 0, 0], [1.0, 0, 0], [5.0, 0, 0]])
		other = np.array([[0.1, 0, 0], [0.05, 0, 0], [1.2, 0, 0], [9.0, 0, 0]])
		pairs = match_objects(reference, other, radius=0.3)
		self.assertEqual([(i, j) for i, j, _ in pairs], [(0, 1), (1, 2)])
		self.assertEqual(match_objects(reference, np.zeros((0, 3)), 0.3), [])

	def test_description_agreement_ignores_filler_words(self) -> None:
		self.assertEqual(jaccard("A brown teddy bear with a slightly visible edge.", "The teddy bear is brown"), 1.0)
		self.assertAlmostEqual(jaccard("A brown teddy bear with a white ear.", "The teddy bear is brown"), 3 / 5)
		self.assertEqual(jaccard("A black office chair", "A white ceramic mug"), 0.0)
		self.assertEqual(jaccard("", ""), 0.0)

	def test_report_counts_matches_against_the_reference(self) -> None:
		reference = {"run": "a", "positions": np.array([[0.0, 0, 0], [2.0, 0, 0]]),
			"descriptions": ["a red chair", "a blue mug"], "counts": {}}
		other = {"run": "b", "positions": np.array([[0.1, 0, 0]]), "descriptions": ["red chair"], "counts": {"objects": 1}}
		result = compare(reference, other, radius=0.3)
		self.assertEqual(result["matched"], 1)
		self.assertAlmostEqual(result["matched_share_of_reference"], 0.5)
		self.assertAlmostEqual(result["matched_share_of_run"], 1.0)
		self.assertAlmostEqual(result["description_jaccard"], 1.0)
		self.assertEqual(result["examples"][0]["run"], "red chair")


if __name__ == "__main__":
	unittest.main()
