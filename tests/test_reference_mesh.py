import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np


REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("build_reference_mesh", REPO / "scripts" / "build_reference_mesh.py")
module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = module  # dataclasses look their module up here
_spec.loader.exec_module(module)

try:
	module._import_open3d()  # the way the builder imports it: a wheel installed without its dependencies works too
	HAS_OPEN3D = True
except ImportError:
	HAS_OPEN3D = False

WIDTH, HEIGHT = 32, 24
INTRINSICS = [[30.0, 0.0, 16.0], [0.0, 30.0, 12.0], [0.0, 0.0, 1.0]]


def pose(tx: float = 0.0) -> np.ndarray:
	"""T_world_camera: the camera sits at x = tx and looks along the world z axis."""
	matrix = np.eye(4)
	matrix[0, 3] = tx
	return matrix


def write_dataset(root: Path, poses=None, depth_value: int = 1000, write_images: bool = True) -> Path:
	"""A prepared dataset (rgb/, depth/, pose/poses.txt, timestamps.txt, camera_info.json) with a flat wall 1 m away."""
	import cv2

	poses = [pose(0.0), pose(0.1)] if poses is None else poses
	for folder in ("rgb", "depth", "pose"):
		(root / folder).mkdir(parents=True)
	(root / "camera_info.json").write_text(json.dumps({"width": WIDTH, "height": HEIGHT, "intrinsics": INTRINSICS, "depth_scale": 1000.0, "fps": 15.0}))
	(root / "timestamps.txt").write_text("".join(f"{k / 15.0:.6f}\n" for k in range(len(poses))))
	(root / "pose" / "poses.txt").write_text("".join(" ".join(f"{v:.9g}" for v in p.reshape(-1)) + "\n" for p in poses))
	for k in range(len(poses)):
		if write_images:
			colour = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
			colour[..., 0], colour[..., 1], colour[..., 2] = 40 + 5 * k, 120, 200   # BGR on disk
			cv2.imwrite(str(root / "rgb" / f"{k:06d}.png"), colour)
			cv2.imwrite(str(root / "depth" / f"{k:06d}.png"), np.full((HEIGHT, WIDTH), depth_value, np.uint16))
		else:
			(root / "rgb" / f"{k:06d}.png").write_bytes(b"rgb%d" % k)
			(root / "depth" / f"{k:06d}.png").write_bytes(b"depth%d" % k)
	return root


def tiny_arrays() -> dict:
	return {
		"positions": np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], np.float32),
		"colors": np.array([[255, 0, 0], [0, 255, 0], [0, 0, 255], [9, 9, 9]], np.uint8),
		"normals": np.array([[0, 0, 1]] * 4, np.float32),
		"triangles": np.array([[0, 1, 2], [0, 1, 3]], np.uint32),
	}


class ReferenceMeshCacheTest(unittest.TestCase):
	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.dataset = write_dataset(Path(self.temp.name) / "tum_x", write_images=False)
		self.config = module.ReferenceConfig(0.01, 0.04, 4.0)

	def tearDown(self) -> None:
		self.temp.cleanup()

	def fingerprint(self, config=None) -> str:
		return module.dataset_fingerprint(module.dataset_inventory(self.dataset), config or self.config)

	def test_defaults_are_one_centimetre_voxels_four_centimetre_truncation_and_four_metres(self) -> None:
		default = module.ReferenceConfig()
		self.assertEqual((default.voxel_length, default.sdf_trunc, default.max_depth), (0.01, 0.04, 4.0))

	def test_fingerprint_is_deterministic(self) -> None:
		self.assertEqual(self.fingerprint(), self.fingerprint())
		self.assertEqual(len(self.fingerprint()), 64)

	def test_every_relevant_change_invalidates_the_fingerprint(self) -> None:
		first = self.fingerprint()
		mutations = {
			"pose": lambda: (self.dataset / "pose" / "poses.txt").write_text("changed\n"),
			"camera": lambda: (self.dataset / "camera_info.json").write_text(json.dumps({"width": WIDTH, "height": HEIGHT, "intrinsics": INTRINSICS, "depth_scale": 5000.0})),
			"timestamp": lambda: (self.dataset / "timestamps.txt").write_text("0.0\n9.0\n"),
			"file name": lambda: (self.dataset / "rgb" / "000001.png").rename(self.dataset / "rgb" / "000002.png"),
			"file size": lambda: (self.dataset / "depth" / "000000.png").write_bytes(b"a different length"),
			"modification time": lambda: os.utime(self.dataset / "rgb" / "000000.png", ns=(1_000_000_000, 2_000_000_000)),
			"extra frame": lambda: (self.dataset / "depth" / "000005.png").write_bytes(b"x"),
		}
		for name, mutate in mutations.items():
			with self.subTest(change=name):
				snapshot = tempfile.mkdtemp(dir=self.temp.name)
				shutil.copytree(self.dataset, Path(snapshot) / "copy", copy_function=shutil.copy2)
				mutate()
				self.assertNotEqual(first, self.fingerprint(), name)
				shutil.rmtree(self.dataset)
				shutil.copytree(Path(snapshot) / "copy", self.dataset, copy_function=shutil.copy2)
				self.assertEqual(first, self.fingerprint(), f"{name}: the restored dataset must match again")

	def test_every_reconstruction_parameter_invalidates_the_fingerprint(self) -> None:
		first = self.fingerprint()
		for changed in (module.ReferenceConfig(0.02, 0.04, 4.0), module.ReferenceConfig(0.01, 0.05, 4.0), module.ReferenceConfig(0.01, 0.04, 5.0)):
			self.assertNotEqual(first, self.fingerprint(changed), changed)

	def test_default_cache_location_and_override(self) -> None:
		npz, manifest = module.resolve_cache_paths(self.dataset, None)
		self.assertEqual(npz, self.dataset / ".daaam/reference_mesh/open3d_tsdf_1cm.npz")
		self.assertEqual(manifest, self.dataset / ".daaam/reference_mesh/open3d_tsdf_1cm.json")
		other = Path(self.temp.name) / "elsewhere"
		self.assertEqual(module.resolve_cache_paths(self.dataset, other), (other / "open3d_tsdf_1cm.npz", other / "open3d_tsdf_1cm.json"))

	def publish(self, **kwargs) -> Path:
		stats = {"integrated": 2, "skipped": 0}
		return module.publish_cache(self.dataset, self.config, tiny_arrays(), stats, **kwargs)

	def test_a_published_cache_is_reused_until_the_dataset_or_parameters_change(self) -> None:
		self.assertIsNone(module.load_valid_cache(self.dataset, self.config))
		npz = self.publish()
		self.assertEqual(module.load_valid_cache(self.dataset, self.config), npz)
		self.assertIsNone(module.load_valid_cache(self.dataset, module.ReferenceConfig(0.02, 0.04, 4.0)))
		(self.dataset / "pose" / "poses.txt").write_text("changed\n")
		self.assertIsNone(module.load_valid_cache(self.dataset, self.config))

	def test_publication_writes_a_complete_manifest_and_no_temporary_files(self) -> None:
		npz = self.publish()
		manifest = json.loads(npz.with_suffix(".json").read_text())
		self.assertEqual(manifest["schema"], module.CACHE_SCHEMA)
		self.assertEqual(manifest["parameters"]["voxel_length"], 0.01)
		self.assertEqual(manifest["parameters"]["sdf_trunc"], 0.04)
		self.assertEqual(manifest["parameters"]["max_depth"], 4.0)
		self.assertEqual(manifest["parameters"]["pose_convention"], "T_world_camera")
		self.assertEqual(manifest["mesh"]["vertices"], 4)
		self.assertEqual(manifest["mesh"]["triangles"], 2)
		self.assertEqual(manifest["frames"], {"rgb": 2, "depth": 2, "integrated": 2, "skipped": 0})
		self.assertEqual(manifest["fingerprint"], self.fingerprint())
		self.assertEqual(manifest["dataset"], str(self.dataset.resolve()))
		self.assertEqual(sorted(p.name for p in npz.parent.iterdir()), ["open3d_tsdf_1cm.json", "open3d_tsdf_1cm.npz"])

	def test_a_cache_in_an_override_directory_is_found_there_and_the_dataset_stays_untouched(self) -> None:
		override = Path(self.temp.name) / "cache"
		npz = self.publish(cache_dir=override)
		self.assertEqual(npz.parent, override)
		self.assertIsNone(module.load_valid_cache(self.dataset, self.config))
		self.assertEqual(module.load_valid_cache(self.dataset, self.config, override), npz)
		self.assertFalse((self.dataset / ".daaam").exists())

	def test_a_corrupt_stale_or_mismatched_cache_is_rejected(self) -> None:
		npz = self.publish()
		manifest_path = npz.with_suffix(".json")
		good_manifest = manifest_path.read_text()
		good_npz = npz.read_bytes()

		manifest_path.write_text("{not json")
		self.assertIsNone(module.load_valid_cache(self.dataset, self.config), "corrupt manifest")
		stale = json.loads(good_manifest)
		stale["schema"] = module.CACHE_SCHEMA + 1
		manifest_path.write_text(json.dumps(stale))
		self.assertIsNone(module.load_valid_cache(self.dataset, self.config), "other schema")
		manifest_path.write_text(good_manifest)
		self.assertEqual(module.load_valid_cache(self.dataset, self.config), npz)

		npz.write_bytes(good_npz[: len(good_npz) // 2])
		self.assertIsNone(module.load_valid_cache(self.dataset, self.config), "truncated arrays")
		npz.write_bytes(good_npz[:-1] + bytes([good_npz[-1] ^ 1]))
		self.assertIsNone(module.load_valid_cache(self.dataset, self.config), "flipped bit")
		npz.write_bytes(good_npz)
		manifest_path.unlink()
		self.assertIsNone(module.load_valid_cache(self.dataset, self.config), "missing manifest")

	def test_arrays_must_form_a_finite_indexed_triangle_mesh(self) -> None:
		module.validate_arrays(tiny_arrays())
		cases = {
			"missing member": lambda a: a.pop("normals"),
			"wrong width": lambda a: a.update(positions=np.zeros((4, 2), np.float32)),
			"wrong dtype": lambda a: a.update(triangles=a["triangles"].astype(np.int64)),
			"nan vertex": lambda a: a["positions"].__setitem__((1, 1), np.nan),
			"infinite normal": lambda a: a["normals"].__setitem__((0, 0), np.inf),
			"index out of range": lambda a: a["triangles"].__setitem__((0, 0), 4),
			"length mismatch": lambda a: a.update(colors=a["colors"][:3]),
			"no triangles": lambda a: a.update(triangles=np.zeros((0, 3), np.uint32)),
			"no vertices": lambda a: a.update(positions=np.zeros((0, 3), np.float32), colors=np.zeros((0, 3), np.uint8), normals=np.zeros((0, 3), np.float32)),
		}
		for name, damage in cases.items():
			with self.subTest(case=name):
				arrays = tiny_arrays()
				damage(arrays)
				with self.assertRaises(ValueError):
					module.validate_arrays(arrays)

	def test_an_invalid_mesh_is_never_published(self) -> None:
		arrays = tiny_arrays()
		arrays["triangles"][0, 0] = 99
		with self.assertRaises(ValueError):
			module.publish_cache(self.dataset, self.config, arrays, {"integrated": 1, "skipped": 0})
		self.assertFalse((self.dataset / ".daaam").exists())


FAKE_OPEN3D_PACKAGE = '''
import os
import types
import numpy as np

__version__ = "fake"
print("[Open3D INFO] python-level noise")
os.write(1, b"[Open3D WARNING] native noise\\n")


class _Mesh:
    vertices = np.array([[0.0, 0.0, 1.0], [0.1, 0.0, 1.0], [0.0, 0.1, 1.0]])
    vertex_colors = np.full((3, 3), 0.5)
    triangles = np.array([[0, 1, 2]])
    vertex_normals = np.zeros((0, 3))

    def remove_duplicated_vertices(self): return self
    remove_duplicated_triangles = remove_degenerate_triangles = remove_unreferenced_vertices = remove_duplicated_vertices

    def compute_vertex_normals(self):
        self.vertex_normals = np.tile([0.0, 0.0, -1.0], (3, 1))
        return self


class _Volume:
    def __init__(self, **kwargs): pass
    def integrate(self, rgbd, intrinsic, extrinsic): pass
    def extract_triangle_mesh(self): return _Mesh()


class _Image:
    def __init__(self, array): self.array = array


class _RGBDImage:
    @staticmethod
    def create_from_color_and_depth(color, depth, **kwargs): return types.SimpleNamespace(color=color, depth=depth)


camera = types.SimpleNamespace(PinholeCameraIntrinsic=lambda *args: args)
geometry = types.SimpleNamespace(Image=_Image, RGBDImage=_RGBDImage)
pipelines = types.SimpleNamespace(integration=types.SimpleNamespace(ScalableTSDFVolume=_Volume, TSDFVolumeColorType=types.SimpleNamespace(RGB8="rgb8")))
'''


class FakeOpen3D:
	"""Just enough of Open3D to see what the builder hands to the integration boundary."""

	def __init__(self, vertices=None) -> None:
		self.extrinsics = []
		self.integrated_colours = []
		self.volume_args = None
		self.vertices = np.array([[0.0, 0.0, 1.0], [0.1, 0.0, 1.0], [0.0, 0.1, 1.0]]) if vertices is None else vertices
		outer = self

		class Mesh:
			def __init__(self) -> None:
				self.vertices = outer.vertices
				self.vertex_colors = np.full((len(outer.vertices), 3), 0.5)
				self.vertex_normals = np.zeros((0, 3))
				self.triangles = np.array([[0, 1, 2]]) if len(outer.vertices) >= 3 else np.zeros((0, 3), np.int32)

			def remove_duplicated_vertices(self):
				return self

			remove_duplicated_triangles = remove_degenerate_triangles = remove_unreferenced_vertices = remove_duplicated_vertices

			def compute_vertex_normals(self):
				self.vertex_normals = np.tile([0.0, 0.0, -1.0], (len(self.vertices), 1))
				return self

		class Volume:
			def __init__(self, **kwargs) -> None:
				outer.volume_args = kwargs

			def integrate(self, rgbd, intrinsic, extrinsic) -> None:
				outer.extrinsics.append(np.array(extrinsic))
				outer.integrated_colours.append(np.array(rgbd.color))

			def extract_triangle_mesh(self):
				return Mesh()

		class Image:
			def __init__(self, array) -> None:
				self.array = np.array(array)

		class RGBDImage:
			@staticmethod
			def create_from_color_and_depth(color, depth, **kwargs):
				RGBDImage.kwargs = kwargs
				return types.SimpleNamespace(color=color.array, depth=depth.array)

		self.RGBDImage = RGBDImage
		self.module = types.SimpleNamespace(
			__version__="fake",
			camera=types.SimpleNamespace(PinholeCameraIntrinsic=lambda *args: types.SimpleNamespace(args=args)),
			geometry=types.SimpleNamespace(Image=Image, RGBDImage=RGBDImage),
			pipelines=types.SimpleNamespace(integration=types.SimpleNamespace(
				ScalableTSDFVolume=Volume,
				TSDFVolumeColorType=types.SimpleNamespace(RGB8="rgb8"),
			)),
		)


class ReferenceMeshBuildTest(unittest.TestCase):
	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.dataset = write_dataset(Path(self.temp.name) / "tum_x")
		self.config = module.ReferenceConfig()

	def tearDown(self) -> None:
		self.temp.cleanup()

	def test_poses_are_inverted_before_integration(self) -> None:
		fake = FakeOpen3D()
		with mock.patch.object(module, "_import_open3d", return_value=fake.module), contextlib.redirect_stderr(io.StringIO()):
			inputs = module.read_prepared_dataset(self.dataset)
			stats = {}
			arrays = module.integrate_reference_mesh(inputs, self.config, stats)
		self.assertEqual(len(fake.extrinsics), 2)
		np.testing.assert_allclose(fake.extrinsics[0], np.linalg.inv(pose(0.0)))
		np.testing.assert_allclose(fake.extrinsics[1], np.linalg.inv(pose(0.1)))
		self.assertAlmostEqual(fake.extrinsics[1][0, 3], -0.1)
		self.assertEqual(stats, {"integrated": 2, "skipped": 0})
		module.validate_arrays(arrays)
		self.assertEqual(arrays["normals"].dtype, np.float32)

	def test_parameters_and_image_conversion_reach_open3d(self) -> None:
		fake = FakeOpen3D()
		with mock.patch.object(module, "_import_open3d", return_value=fake.module), contextlib.redirect_stderr(io.StringIO()):
			module.integrate_reference_mesh(module.read_prepared_dataset(self.dataset), self.config, {})
		self.assertEqual(fake.volume_args, {"voxel_length": 0.01, "sdf_trunc": 0.04, "color_type": "rgb8"})
		self.assertEqual(fake.RGBDImage.kwargs, {"depth_scale": 1000.0, "depth_trunc": 4.0, "convert_rgb_to_intensity": False})
		# the files hold BGR, Open3D wants RGB
		np.testing.assert_array_equal(fake.integrated_colours[0][0, 0], [200, 120, 40])

	def test_frames_without_a_pose_or_picture_are_skipped_and_counted(self) -> None:
		write_bad = Path(self.temp.name) / "tum_bad"
		write_dataset(write_bad, poses=[pose(0.0), pose(0.1), pose(0.2)])
		poses_file = write_bad / "pose" / "poses.txt"
		lines = poses_file.read_text().splitlines()
		lines[1] = " ".join(["nan"] * 16)   # frame 1: no finite pose
		poses_file.write_text("\n".join(lines) + "\n")
		(write_bad / "depth" / "000002.png").write_bytes(b"not a picture")   # frame 2: undecodable depth
		fake = FakeOpen3D()
		errors = io.StringIO()
		with mock.patch.object(module, "_import_open3d", return_value=fake.module), contextlib.redirect_stderr(errors):
			stats = {}
			module.integrate_reference_mesh(module.read_prepared_dataset(write_bad), self.config, stats)
		self.assertEqual(stats, {"integrated": 1, "skipped": 2})
		self.assertIn("skipped 2 of 3", errors.getvalue())
		self.assertIn("000001", errors.getvalue())

	def test_no_integrated_frame_is_fatal(self) -> None:
		broken = Path(self.temp.name) / "tum_broken"
		write_dataset(broken, poses=[pose(0.0)])
		(broken / "pose" / "poses.txt").write_text(" ".join(["inf"] * 16) + "\n")
		with mock.patch.object(module, "_import_open3d", return_value=FakeOpen3D().module), contextlib.redirect_stderr(io.StringIO()):
			with self.assertRaisesRegex(ValueError, "no frame"):
				module.integrate_reference_mesh(module.read_prepared_dataset(broken), self.config, {})

	def test_an_empty_reconstruction_is_fatal(self) -> None:
		fake = FakeOpen3D(vertices=np.zeros((0, 3)))
		with mock.patch.object(module, "_import_open3d", return_value=fake.module), contextlib.redirect_stderr(io.StringIO()):
			with self.assertRaisesRegex(ValueError, "empty"):
				module.integrate_reference_mesh(module.read_prepared_dataset(self.dataset), self.config, {})

	def test_calibration_and_dimension_errors_are_fatal(self) -> None:
		info = self.dataset / "camera_info.json"
		for name, content in {
			"missing key": {"width": WIDTH, "height": HEIGHT, "depth_scale": 1000.0},
			"bad intrinsics": {"width": WIDTH, "height": HEIGHT, "intrinsics": [[0, 0, 1], [0, 30, 12], [0, 0, 1]], "depth_scale": 1000.0},
			"bad depth scale": {"width": WIDTH, "height": HEIGHT, "intrinsics": INTRINSICS, "depth_scale": 0},
		}.items():
			with self.subTest(case=name):
				info.write_text(json.dumps(content))
				with self.assertRaises(ValueError):
					module.read_prepared_dataset(self.dataset)
		info.write_text(json.dumps({"width": 64, "height": HEIGHT, "intrinsics": INTRINSICS, "depth_scale": 1000.0}))
		with mock.patch.object(module, "_import_open3d", return_value=FakeOpen3D().module), contextlib.redirect_stderr(io.StringIO()):
			with self.assertRaisesRegex(ValueError, "000000.*32x24|32x24.*000000"):
				module.integrate_reference_mesh(module.read_prepared_dataset(self.dataset), self.config, {})

	def build(self, fake=None, **kwargs) -> Path:
		fake = fake or FakeOpen3D()
		with mock.patch.object(module, "_import_open3d", return_value=fake.module), contextlib.redirect_stderr(io.StringIO()):
			return module.build_or_reuse(self.dataset, self.config, **kwargs)

	def test_second_build_reuses_the_cache_without_touching_it_or_importing_open3d(self) -> None:
		npz = self.build()
		before = (npz.stat().st_mtime_ns, npz.with_suffix(".json").stat().st_mtime_ns)
		with mock.patch.object(module, "_import_open3d", side_effect=AssertionError("Open3D must not be imported to reuse a cache")), contextlib.redirect_stderr(io.StringIO()):
			again = module.build_or_reuse(self.dataset, self.config)
		self.assertEqual(again, npz)
		self.assertEqual(before, (npz.stat().st_mtime_ns, npz.with_suffix(".json").stat().st_mtime_ns))

	def test_a_corrupt_manifest_or_force_rebuilds(self) -> None:
		npz = self.build()
		npz.with_suffix(".json").write_text("{broken")
		repaired = FakeOpen3D()
		self.assertEqual(self.build(repaired), npz)
		self.assertEqual(len(repaired.extrinsics), 2, "a corrupt manifest means the frames are integrated again")
		self.assertEqual(json.loads(npz.with_suffix(".json").read_text())["frames"]["integrated"], 2)
		untouched, forced = FakeOpen3D(), FakeOpen3D()
		self.build(untouched)
		self.assertEqual(untouched.extrinsics, [], "a valid cache is reused")
		self.build(forced, force=True)
		self.assertEqual(len(forced.extrinsics), 2, "force integrates the frames again although the cache is valid")

	def test_cli_prints_only_the_cache_path_on_stdout(self) -> None:
		fake = FakeOpen3D()
		out, err = io.StringIO(), io.StringIO()
		with mock.patch.object(module, "_import_open3d", return_value=fake.module), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			code = module.main([str(self.dataset)])
		self.assertEqual(code, 0, err.getvalue())
		self.assertEqual(out.getvalue().strip(), str(self.dataset / ".daaam/reference_mesh/open3d_tsdf_1cm.npz"))
		self.assertIn("integrated", err.getvalue())

	def test_log_lines_of_native_libraries_never_reach_stdout(self) -> None:
		fake = Path(self.temp.name) / "fake"
		(fake / "open3d").mkdir(parents=True)
		(fake / "open3d" / "__init__.py").write_text(FAKE_OPEN3D_PACKAGE)
		env = {**os.environ, "PYTHONPATH": str(fake)}
		result = subprocess.run([sys.executable, str(REPO / "scripts" / "build_reference_mesh.py"), str(self.dataset)], capture_output=True, text=True, env=env)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(result.stdout.strip(), str(self.dataset / ".daaam/reference_mesh/open3d_tsdf_1cm.npz"), "stdout carries the path and nothing else")
		self.assertIn("native noise", result.stderr)
		self.assertIn("python-level noise", result.stderr)

	def import_fake_open3d(self, source: str) -> subprocess.CompletedProcess:
		"""Import a made-up `open3d` package through the builder's _import_open3d in a fresh interpreter (a real Open3D, once
		imported, cannot be imported a second time into the same process)."""
		fake = Path(self.temp.name) / "fake_site"
		(fake / "open3d").mkdir(parents=True, exist_ok=True)
		(fake / "open3d" / "__init__.py").write_text(source)
		script = (
			"import importlib.util, sys\n"
			"spec = importlib.util.spec_from_file_location('build_reference_mesh', sys.argv[1])\n"
			"m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m; spec.loader.exec_module(m)\n"
			"print(m._import_open3d().__version__)\n"
			"print(any(type(finder).__name__ == '_EmptyPackages' for finder in sys.meta_path))\n"
		)
		return subprocess.run(
			[sys.executable, "-c", script, str(REPO / "scripts" / "build_reference_mesh.py")],
			capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(fake)},
		)

	def test_open3d_imports_without_the_packages_of_its_web_visualizer_and_ml_toolkit(self) -> None:
		result = self.import_fake_open3d(
			"import plotly.graph_objects as go\nimport dash\nfrom addict import Dict\n\nclass Config(Dict):\n    pass\n\n__version__ = 'needy'\n"
		)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(result.stdout.split(), ["needy", "False"], "imported, and the stand-ins are registered only while Open3D loads")

	def test_other_missing_modules_are_still_reported(self) -> None:
		result = self.import_fake_open3d("import a_module_that_does_not_exist_anywhere\n")
		self.assertNotEqual(result.returncode, 0)
		self.assertIn("a_module_that_does_not_exist_anywhere", result.stderr)

	def test_cli_honours_cache_dir_and_the_environment_override(self) -> None:
		override = Path(self.temp.name) / "cache"
		out = io.StringIO()
		with mock.patch.object(module, "_import_open3d", return_value=FakeOpen3D().module), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
			self.assertEqual(module.main([str(self.dataset), "--cache-dir", str(override)]), 0)
		self.assertEqual(out.getvalue().strip(), str(override / "open3d_tsdf_1cm.npz"))
		second = Path(self.temp.name) / "from-env"
		out = io.StringIO()
		with mock.patch.dict(os.environ, {"REFERENCE_MESH_CACHE": str(second)}), mock.patch.object(module, "_import_open3d", return_value=FakeOpen3D().module), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
			self.assertEqual(module.main([str(self.dataset)]), 0)
		self.assertEqual(out.getvalue().strip(), str(second / "open3d_tsdf_1cm.npz"))

	def test_missing_open3d_explains_how_to_install_it_and_prints_no_path(self) -> None:
		out, err = io.StringIO(), io.StringIO()
		with mock.patch.object(module, "_import_open3d", side_effect=ImportError("No module named 'open3d'")), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			code = module.main([str(self.dataset)])
		self.assertNotEqual(code, 0)
		self.assertEqual(out.getvalue(), "")
		self.assertIn("open3d==0.19.0", err.getvalue())

	def test_an_unwritable_cache_directory_suggests_the_override(self) -> None:
		blocker = Path(self.temp.name) / "blocker"
		blocker.write_text("a file where a directory is needed")
		out, err = io.StringIO(), io.StringIO()
		with mock.patch.object(module, "_import_open3d", return_value=FakeOpen3D().module), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			code = module.main([str(self.dataset), "--cache-dir", str(blocker / "sub")])
		self.assertNotEqual(code, 0)
		self.assertEqual(out.getvalue(), "")
		self.assertIn("REFERENCE_MESH_CACHE", err.getvalue())

	def test_not_a_prepared_dataset_is_reported(self) -> None:
		err = io.StringIO()
		with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
			code = module.main([str(Path(self.temp.name) / "nowhere")])
		self.assertNotEqual(code, 0)
		self.assertIn("nowhere", err.getvalue())


@unittest.skipUnless(HAS_OPEN3D, "Open3D is not installed (pip install open3d==0.19.0)")
class ReferenceMeshOpen3DTest(unittest.TestCase):
	"""The real thing on a 32x24 wall 1 m in front of a camera that moves 10 cm along +x between its two frames."""

	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.dataset = write_dataset(Path(self.temp.name) / "tum_x")

	def tearDown(self) -> None:
		self.temp.cleanup()

	def test_the_wall_is_reconstructed_at_one_metre_in_the_world_frame(self) -> None:
		with contextlib.redirect_stderr(io.StringIO()):
			npz = module.build_or_reuse(self.dataset, module.ReferenceConfig())
		with np.load(npz) as data:
			arrays = {name: data[name] for name in data.files}
		module.validate_arrays(arrays)
		positions = arrays["positions"]
		self.assertGreater(len(positions), 100)
		self.assertLess(np.abs(positions[:, 2] - 1.0).max(), 0.02)
		# the union of the two views is centred right of the first camera only if poses were inverted correctly
		self.assertGreater(positions[:, 0].max() + positions[:, 0].min(), 0.0)
		self.assertTrue(np.isfinite(arrays["normals"]).all())
		self.assertGreater((np.abs(arrays["normals"][:, 2]) > 0.9).mean(), 0.9)   # a flat wall: normals along z
		manifest = json.loads(npz.with_suffix(".json").read_text())
		self.assertEqual(manifest["frames"]["integrated"], 2)
		self.assertEqual(manifest["versions"]["open3d"], __import__("open3d").__version__)


if __name__ == "__main__":
	unittest.main()
