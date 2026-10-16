import contextlib
import importlib.util
import io
import os
import tarfile
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("fetch_tum", REPO / "scripts" / "fetch_tum.py")
fetch_tum = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch_tum)


class TumNamesTest(unittest.TestCase):
	def test_short_names_map_to_the_official_archives(self) -> None:
		self.assertEqual(
			fetch_tum.archive_url("fr1_room"),
			"https://cvg.cit.tum.de/rgbd/dataset/freiburg1/rgbd_dataset_freiburg1_room.tgz",
		)
		self.assertEqual(fetch_tum.sequence_name("fr3_long_office_household"), "rgbd_dataset_freiburg3_long_office_household")
		for name in fetch_tum.SEQUENCES:
			self.assertTrue(fetch_tum.archive_url(name).endswith(f"{fetch_tum.sequence_name(name)}.tgz"), name)

	def test_names_that_are_not_tum_short_names_are_rejected(self) -> None:
		for name in ("room", "fr4_room", "fr1_", "FR1_room", "fr1_room/../x", "fr1_../x", "../fr1_room"):
			with self.assertRaises(ValueError, msg=name):
				fetch_tum.sequence_name(name)


class TumListTest(unittest.TestCase):
	def test_list_shows_what_is_on_disk(self) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			raw, out = Path(tmp) / "raw", Path(tmp) / "out"
			(out / "tum_fr1_room" / "rgb").mkdir(parents=True)
			(out / "tum_fr1_room" / "camera_info.json").write_text("{}")
			(out / "tum_fr1_room" / "rgb" / "000000.png").write_bytes(b"")
			(out / "tum_fr1_room" / "rgb" / "000001.png").write_bytes(b"")
			(out / "tum_fr9_other" / "rgb").mkdir(parents=True)  # no camera_info.json: not a finished set
			(raw / "rgbd_dataset_freiburg1_desk2").mkdir(parents=True)
			(raw / "rgbd_dataset_freiburg3_walking_xyz.tgz").write_bytes(b"x")
			(out / "tum_fr2_desk").mkdir(parents=True)
			(out / "tum_fr2_desk" / "camera_info.json").write_text("{}")
			buffer = io.StringIO()
			with contextlib.redirect_stdout(buffer):
				fetch_tum.list_sets(raw, out)
			rows = {line.split()[0]: line for line in buffer.getvalue().splitlines() if line.startswith("fr")}
			self.assertIn("ready (2 frames)", rows["fr1_room"])
			self.assertIn("unpacked, not converted", rows["fr1_desk2"])
			self.assertIn("partial download", rows["fr3_walking_xyz"])
			self.assertIn("not downloaded", rows["fr3_long_office_household"])
			self.assertIn("fr2_desk", buffer.getvalue().split("其他已转换的数据集")[1])
			self.assertNotIn("fr9_other", buffer.getvalue())

	def test_get_skips_a_set_that_is_already_converted(self) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			raw, out = Path(tmp) / "raw", Path(tmp) / "out"
			(out / "tum_fr1_room").mkdir(parents=True)
			(out / "tum_fr1_room" / "camera_info.json").write_text("{}")
			with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(urllib.request, "build_opener") as opener:
				fetch_tum.get("fr1_room", raw, out, "", False)
			opener.assert_not_called()
			self.assertFalse(raw.exists())


class _Server:
	"""Serves one payload with Range support; the first response is cut off after `cut` bytes, like a proxy dropping the connection."""

	def __init__(self, payload: bytes, cut: int) -> None:
		starts = self.starts = []

		class Handler(BaseHTTPRequestHandler):
			def do_HEAD(self) -> None:
				self.send_response(200)
				self.send_header("Content-Length", str(len(payload)))
				self.end_headers()

			def do_GET(self) -> None:
				requested = self.headers.get("Range")
				start = int(requested.split("=")[1].split("-")[0]) if requested else 0
				starts.append(start)
				self.send_response(206 if requested else 200)
				self.send_header("Content-Length", str(len(payload) - start))
				if requested:
					self.send_header("Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}")
				self.end_headers()
				body = payload[start:]
				self.wfile.write(body[:cut] if len(starts) == 1 else body)

			def log_message(self, *args) -> None:
				pass

		self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
		self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/x.tgz"
		self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

	def __enter__(self) -> "_Server":
		self.thread.start()
		return self

	def __exit__(self, *exc) -> None:
		self.httpd.shutdown()
		self.httpd.server_close()


class TumDownloadTest(unittest.TestCase):
	def test_download_continues_after_a_dropped_connection(self) -> None:
		payload = os.urandom(3 * 1024 * 1024)
		opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
		with tempfile.TemporaryDirectory() as tmp, _Server(payload, cut=1024 * 1024) as server:
			dest = Path(tmp) / "x.tgz"
			with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(fetch_tum.time, "sleep"):
				size = fetch_tum.remote_size(opener, server.url)
				fetch_tum.download(opener, server.url, dest, size)
			self.assertEqual(size, len(payload))
			self.assertEqual(dest.read_bytes(), payload)
			self.assertEqual(server.starts, [0, 1024 * 1024])  # the second request resumed where the first one stopped


class TumExtractTest(unittest.TestCase):
	def _archive(self, path: Path, members: dict) -> None:
		with tarfile.open(path, "w:gz") as tar:
			for name, data in members.items():
				info = tarfile.TarInfo(name)
				info.size = len(data)
				tar.addfile(info, io.BytesIO(data))

	def test_extract_moves_the_sequence_into_place(self) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			raw = Path(tmp)
			self._archive(raw / "a.tgz", {"rgbd_dataset_freiburg1_x/rgb.txt": b"# rgb\n", "rgbd_dataset_freiburg1_x/rgb/1.png": b"png"})
			fetch_tum.extract(raw / "a.tgz", raw, "rgbd_dataset_freiburg1_x")
			self.assertEqual((raw / "rgbd_dataset_freiburg1_x" / "rgb" / "1.png").read_bytes(), b"png")
			self.assertFalse((raw / ".extract-rgbd_dataset_freiburg1_x").exists())

	def test_extract_refuses_members_outside_the_sequence_folder(self) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			raw = Path(tmp) / "raw"
			raw.mkdir()
			self._archive(raw / "a.tgz", {"rgbd_dataset_freiburg1_x/rgb.txt": b"", "../evil.txt": b"x"})
			with self.assertRaises(SystemExit):
				fetch_tum.extract(raw / "a.tgz", raw, "rgbd_dataset_freiburg1_x")
			self.assertFalse((Path(tmp) / "evil.txt").exists())
			self.assertFalse((raw / "rgbd_dataset_freiburg1_x").exists())

	def test_extract_refuses_an_archive_without_the_expected_sequence(self) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			raw = Path(tmp)
			self._archive(raw / "a.tgz", {"something_else/rgb.txt": b""})
			with self.assertRaises(SystemExit):
				fetch_tum.extract(raw / "a.tgz", raw, "rgbd_dataset_freiburg1_x")


if __name__ == "__main__":
	unittest.main()
