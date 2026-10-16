import unittest
from pathlib import Path

from scripts.replay_web.paths import PathError, normalize_user_path


DISTRO = "Ubuntu-20.04"


class NormalizeUserPathTest(unittest.TestCase):
	def test_windows_and_wsl_unc_paths_normalize_to_linux(self) -> None:
		self.assertEqual(normalize_user_path(r"E:\daaam-data\run", distro=DISTRO), Path("/mnt/e/daaam-data/run"))
		self.assertEqual(normalize_user_path(r"\\wsl.localhost\Ubuntu-20.04\home\me\run", distro=DISTRO), Path("/home/me/run"))

	def test_every_supported_form(self) -> None:
		cases = {
			"/home/me/output/tum_replay/out_1": "/home/me/output/tum_replay/out_1",
			"/mnt/c/Users/me/run": "/mnt/c/Users/me/run",
			"C:/Users/me/run": "/mnt/c/Users/me/run",
			r"c:\Users\me\run\\": "/mnt/c/Users/me/run",
			"D:\\": "/mnt/d",
			r"\\wsl$\Ubuntu-20.04\home\me\run": "/home/me/run",
			"//wsl.localhost/Ubuntu-20.04/home/me/run": "/home/me/run",
			r"\\WSL.LOCALHOST\ubuntu-20.04\home\me": "/home/me",
			r"\\wsl.localhost\Ubuntu-20.04": "/",
			'  "E:\\daaam-data\\output\\coda\\out_1"  ': "/mnt/e/daaam-data/output/coda/out_1",
			"'/home/me/run'": "/home/me/run",
		}
		for raw, expected in cases.items():
			with self.subTest(raw=raw):
				self.assertEqual(normalize_user_path(raw, distro=DISTRO), Path(expected))

	def test_a_unc_path_of_another_distribution_is_rejected(self) -> None:
		with self.assertRaises(PathError) as caught:
			normalize_user_path(r"\\wsl.localhost\Debian\home\me\run", distro=DISTRO)
		self.assertIn("Ubuntu-20.04", str(caught.exception))

	def test_relative_empty_and_foreign_paths_are_rejected(self) -> None:
		for raw in ("", "   ", "output/tum_replay", "./run", "~/run", r"\\fileserver\share\run", "C:relative", "/home/me/\x00x", r"\\wsl.localhost"):
			with self.subTest(raw=raw), self.assertRaises(PathError):
				normalize_user_path(raw, distro=DISTRO)

	def test_dot_components_are_left_for_resolution(self) -> None:
		self.assertEqual(normalize_user_path("/home/me/run/./replay/..", distro=DISTRO), Path("/home/me/run/./replay/.."))


if __name__ == "__main__":
	unittest.main()
