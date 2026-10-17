import contextlib
import io
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path

from scripts.replay_web.manifest import ManifestError
from scripts.replay_web.paths import PathError
from scripts.replay_web.runs import RunRegistry
from tests.replay_fixtures import write_minimal_bundle


DISTRO = "Ubuntu-20.04"


class RunRegistryTest(unittest.TestCase):
	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.root = Path(self.temp.name).resolve()
		self.output = self.root / "output"
		self.output.mkdir()
		self.registry = RunRegistry(output_root=self.output, distro=DISTRO)

	def tearDown(self) -> None:
		self.temp.cleanup()

	def test_open_returns_an_opaque_stable_id_for_the_same_canonical_path(self) -> None:
		result = write_minimal_bundle(self.output / "tum_replay" / "out_20261017_000000")
		first = self.registry.open(str(result))
		second = self.registry.open(str(result / "."))
		self.assertEqual(first.run_id, second.run_id)
		self.assertNotIn(str(result), first.run_id)
		self.assertNotIn("out_20261017_000000", first.run_id)
		self.assertRegex(first.run_id, r"^[A-Za-z0-9_-]{16,}$")
		self.assertEqual(first.canonical_path, result)
		self.assertEqual(first.bundle_root, result / "replay")
		self.assertEqual(first.manifest["dataset"]["kind"], "tum")
		self.assertEqual(self.registry.get(first.run_id).canonical_path, result)

	def test_a_symlink_parent_steps_and_the_bundle_itself_open_the_same_result(self) -> None:
		result = write_minimal_bundle(self.root / "elsewhere" / "out_1")
		(self.root / "link").symlink_to(self.root / "elsewhere")
		base = self.registry.open(str(result)).run_id
		for raw in (str(self.root / "link" / "out_1"), str(result / "replay" / ".."), str(result / "replay"), str(result / "replay" / "manifest.json")):
			with self.subTest(raw=raw):
				opened = self.registry.open(raw)
				self.assertEqual(opened.run_id, base)
				self.assertEqual(opened.canonical_path, result)

	def test_two_results_with_the_same_basename_get_distinct_ids(self) -> None:
		a = write_minimal_bundle(self.output / "tum_replay" / "out_1", title="a")
		b = write_minimal_bundle(self.output / "tum_replay_fr1_room" / "out_1", title="b")
		first, second = self.registry.open(str(a)), self.registry.open(str(b))
		self.assertNotEqual(first.run_id, second.run_id)
		self.assertEqual((first.manifest["title"], second.manifest["title"]), ("a", "b"))

	def test_missing_manifest_missing_directory_and_files_are_rejected_without_registering(self) -> None:
		plain = self.output / "plain"
		plain.mkdir()
		(self.root / "file.txt").write_text("x", encoding="utf-8")
		with self.assertRaises(ManifestError) as caught:
			self.registry.open(str(plain))
		self.assertIn(str(plain / "replay" / "manifest.json"), str(caught.exception))
		for raw in (str(self.root / "missing"), str(self.root / "file.txt"), "relative/path", r"\\wsl.localhost\Debian\home"):
			with self.subTest(raw=raw), self.assertRaises(PathError):
				self.registry.open(raw)
		self.assertEqual(self.registry.runs(), [])

	def test_an_invalid_manifest_is_reported_and_other_results_stay_open(self) -> None:
		good = self.registry.open(str(write_minimal_bundle(self.output / "good")))
		bad = write_minimal_bundle(self.output / "bad")
		(bad / "replay" / "timeline" / "000001.json.gz").unlink()
		with self.assertRaises(ManifestError) as caught:
			self.registry.open(str(bad))
		self.assertIn("timeline/000001.json.gz", str(caught.exception))
		self.assertEqual([run.run_id for run in self.registry.runs()], [good.run_id])

	def test_close_releases_the_id_but_keeps_the_files(self) -> None:
		result = write_minimal_bundle(self.output / "out_1")
		opened = self.registry.open(str(result))
		self.assertTrue(self.registry.close(opened.run_id))
		self.assertFalse(self.registry.close(opened.run_id))
		with self.assertRaises(KeyError):
			self.registry.get(opened.run_id)
		self.assertTrue((result / "replay" / "manifest.json").is_file())
		self.assertNotEqual(self.registry.open(str(result)).run_id, opened.run_id, "a closed id is never reused")

	def test_reopening_a_republished_bundle_refreshes_its_manifest(self) -> None:
		result = write_minimal_bundle(self.output / "out_1", title="first")
		first = self.registry.open(str(result))
		write_minimal_bundle(result, title="second", snapshots=2)
		second = self.registry.open(str(result))
		self.assertEqual(second.run_id, first.run_id)
		self.assertEqual(second.manifest["title"], "second")
		self.assertEqual(self.registry.get(first.run_id).manifest["snapshots"]["count"], 2)

	def test_listeners_hear_opens_and_closes_and_a_failing_listener_breaks_nothing(self) -> None:
		result = write_minimal_bundle(self.output / "out_1")
		heard = []

		def listener(event: str, run) -> None:
			heard.append((event, run.run_id))

		def failing(event: str, run) -> None:
			raise RuntimeError("listener failed")

		self.registry.add_listener(failing)
		self.registry.add_listener(listener)
		with contextlib.redirect_stderr(io.StringIO()) as log:
			opened = self.registry.open(str(result))
			self.registry.open(str(result))
			self.assertTrue(self.registry.close(opened.run_id))
			self.assertFalse(self.registry.close(opened.run_id))
		self.assertEqual(heard, [("open", opened.run_id), ("open", opened.run_id), ("close", opened.run_id)])
		self.assertIn("listener failed", log.getvalue())
		self.registry.remove_listener(listener)
		self.registry.remove_listener(failing)
		self.registry.remove_listener(failing)
		self.registry.open(str(result))
		self.assertEqual(len(heard), 3)

	def test_concurrent_opens_of_one_result_share_one_id(self) -> None:
		result = write_minimal_bundle(self.output / "out_1")
		ids, errors = [], []

		def worker() -> None:
			try:
				ids.append(self.registry.open(str(result)).run_id)
			except Exception as error:   # pragma: no cover - reported below
				errors.append(error)

		threads = [threading.Thread(target=worker) for _ in range(8)]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join()
		self.assertEqual(errors, [])
		self.assertEqual(len(set(ids)), 1)

	def test_discover_lists_each_valid_result_once_newest_first_without_opening_it(self) -> None:
		older = write_minimal_bundle(self.output / "tum_replay" / "out_20261016_000000", title="older")
		coda_root = self.root / "drive" / "coda"
		newer = write_minimal_bundle(coda_root / "out_20261017_000000", dataset_kind="coda", sequence="0", title="newer")
		(self.output / "coda").symlink_to(coda_root)
		(self.output / "alias").symlink_to(self.output / "tum_replay")
		os.utime(older / "replay" / "manifest.json", (1000, 1000))
		broken = self.output / "tum_replay" / "out_broken" / "replay"
		broken.mkdir(parents=True)
		(broken / "manifest.json").write_text("{", encoding="utf-8")
		write_minimal_bundle(self.output / "tum_replay" / ".replay.123.tmp" / "inner")
		found = self.registry.discover()
		self.assertEqual([item.canonical_path for item in found], [newer, older])
		self.assertEqual([item.title for item in found], ["newer", "older"])
		self.assertEqual(found[0].dataset_kind, "coda")
		self.assertEqual(found[0].sequence, "0")
		self.assertEqual(found[0].snapshot_count, 3)
		self.assertEqual(found[1].relative_path, "tum_replay/out_20261016_000000")
		self.assertEqual(self.registry.runs(), [], "discovery assigns no run IDs")
		summary = found[0].to_json()
		self.assertEqual(summary["path"], str(newer))
		self.assertNotIn("run_id", summary)
		json.dumps(summary)

	def test_discovery_depth_is_bounded_and_survives_symlink_loops(self) -> None:
		registry = RunRegistry(output_root=self.output, distro=DISTRO, max_depth=3)
		write_minimal_bundle(self.output / "a" / "b" / "c")
		write_minimal_bundle(self.output / "a" / "b" / "c" / "d" / "e")
		(self.output / "a" / "loop").symlink_to(self.output / "a")
		found = registry.discover()
		self.assertEqual([item.relative_path for item in found], ["a/b/c"])

	def test_a_missing_output_root_discovers_nothing(self) -> None:
		registry = RunRegistry(output_root=self.root / "no-output", distro=DISTRO)
		self.assertEqual(registry.discover(), [])


if __name__ == "__main__":
	unittest.main()
