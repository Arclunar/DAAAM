import contextlib
import dataclasses
import hashlib
import io
import json
import os
import queue
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest import mock

from scripts.replay_web import artifacts
from scripts.replay_web.artifacts import (
	GRAPH_PRIORITY, ArtifactChanging, ArtifactError, ArtifactMonitor, ArtifactVersion, load_valid_graph,
	normalize_graph, select_active_graph,
)
from scripts.replay_web.runs import RunRegistry
from tests.replay_fixtures import graph_document, write_graph, write_minimal_bundle

DISTRO = "Ubuntu-20.04"


class GraphSelectionTest(unittest.TestCase):
	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.result = Path(self.temp.name).resolve() / "out_1"
		self.result.mkdir()

	def tearDown(self) -> None:
		self.temp.cleanup()

	def test_the_priority_is_the_approved_order(self) -> None:
		self.assertEqual(GRAPH_PRIORITY, ("clustered_dsg_with_summaries.json", "clustered_dsg.json", "dsg_updated.json", "dsg.json"))

	def test_best_valid_graph_wins_and_invalid_newer_candidate_does_not_replace_it(self) -> None:
		result = self.result
		write_graph(result / "dsg.json", marker="raw")
		write_graph(result / "clustered_dsg.json", marker="clustered")
		graph, version = select_active_graph(result)
		self.assertEqual((graph["marker"], version.name), ("clustered", "clustered_dsg.json"))
		(result / "clustered_dsg_with_summaries.json").write_text("{")
		graph, version = select_active_graph(result)
		self.assertEqual((graph["marker"], version.name), ("clustered", "clustered_dsg.json"))

	def test_a_result_without_any_candidate_has_no_graph(self) -> None:
		(self.result / "dsg.json.tmp").write_text("{}")
		self.assertIsNone(select_active_graph(self.result))

	def test_malformed_or_non_graph_json_is_rejected(self) -> None:
		path = self.result / "dsg.json"
		for content in (b"", b"{", b"[]", b'"text"', b'{"nodes": {}}', b'{"edges": []}', b'{"nodes": [{"id": "O1", "layer": 2}]}',
				b'{"nodes": [{"id": 1}]}', b'{"nodes": [], "edges": [{"source": 1}]}', b"\xff\xfe{}"):
			with self.subTest(content=content):
				path.write_bytes(content)
				with self.assertRaises(ArtifactError) as caught:
					load_valid_graph(path)
				self.assertIn("dsg.json", str(caught.exception))
				self.assertNotIn(str(self.result), str(caught.exception), "errors reach the browser: no host paths")
		write_graph(self.result / "dsg_updated.json", marker="updated")
		graph, version = select_active_graph(self.result)
		self.assertEqual(version.name, "dsg_updated.json", "an invalid file falls through to the next candidate")

	def test_the_version_records_size_mtime_and_content_digest(self) -> None:
		data = write_graph(self.result / "dsg.json")
		graph, version = load_valid_graph(self.result / "dsg.json")
		self.assertIsInstance(version, ArtifactVersion)
		stat = os.stat(self.result / "dsg.json")
		self.assertEqual((version.name, version.path, version.size, version.mtime_ns), ("dsg.json", self.result / "dsg.json", len(data), stat.st_mtime_ns))
		self.assertEqual(version.digest, hashlib.sha256(data).hexdigest())
		with self.assertRaises(dataclasses.FrozenInstanceError):
			version.size = 0   # type: ignore[misc]
		summary = version.to_json()
		self.assertEqual(summary["name"], "dsg.json")
		self.assertEqual(summary["rank"], 3)
		self.assertNotIn("path", summary)
		self.assertEqual(json.loads(json.dumps(summary)), summary)

	def test_a_file_that_changes_while_it_is_read_is_reported_as_changing(self) -> None:
		write_graph(self.result / "dsg.json")
		real_stat = os.stat

		def later_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
			value = real_stat(path, *args, **kwargs)
			return FakeStat(value, st_mtime_ns=value.st_mtime_ns + 1)

		with mock.patch.object(artifacts.os, "stat", side_effect=later_stat), self.assertRaises(ArtifactChanging):
			load_valid_graph(self.result / "dsg.json")

	def test_size_limit_and_symlinks_out_of_the_result_are_refused(self) -> None:
		write_graph(self.result / "dsg.json")
		with self.assertRaises(ArtifactError) as caught:
			load_valid_graph(self.result / "dsg.json", max_bytes=100)
		self.assertIn("100", str(caught.exception))
		outside = self.result.parent / "elsewhere.json"
		write_graph(outside)
		(self.result / "dsg_updated.json").symlink_to(outside)
		with self.assertRaises(ArtifactError) as caught:
			load_valid_graph(self.result / "dsg_updated.json", root=self.result)
		self.assertIn("outside", str(caught.exception))
		inside = self.result / "hydra_output"
		inside.mkdir()
		write_graph(inside / "dsg.json", marker="inside")
		(self.result / "clustered_dsg.json").symlink_to(inside / "dsg.json")
		graph, version = load_valid_graph(self.result / "clustered_dsg.json", root=self.result)
		self.assertEqual(graph["marker"], "inside")


class ErrorMessageTest(unittest.TestCase):
	"""Artifact errors are sent to the browser: they name files, never host paths."""

	def test_a_symbolic_link_loop_is_reported_without_its_path(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			result = Path(temp).resolve() / "out_1"
			result.mkdir()
			loop = result / "dsg.json"
			loop.symlink_to(loop)
			with self.assertRaises(ArtifactError) as caught:
				load_valid_graph(loop, root=result)
			self.assertEqual(str(caught.exception), "dsg.json cannot be resolved (symbolic link loop)")
			self.assertIsNone(select_active_graph(result))
			registry = RunRegistry(output_root=Path(temp), distro=DISTRO)
			write_minimal_bundle(result)
			registry.open(str(result))
			heard = []
			ArtifactMonitor(registry, lambda run_id, kind, payload: heard.append((kind, payload)), settle=0.0).scan_once()
			errors = [payload for kind, payload in heard if kind == "artifact-error"]
			self.assertEqual(len(errors), 1)
			self.assertNotIn(temp, errors[0]["error"])
			self.assertIn("dsg.json", errors[0]["error"])


class NormalizedGraphTest(unittest.TestCase):
	def test_objects_places_and_summarized_rooms_are_kept_without_heavy_or_private_fields(self) -> None:
		document = normalize_graph(graph_document(objects=(0, 3), rooms=True))
		self.assertEqual([item["id"] for item in document["objects"]], ["O0", "O3"])
		first = document["objects"][0]
		self.assertEqual((first["label"], first["description"], first["position"]), (40, "object 0", [0.0, 0.5, 1.0]))
		self.assertEqual((first["place"], first["room"]), ("p7", "R0"))
		self.assertEqual([item["id"] for item in document["places"]], ["p7"])
		self.assertEqual(document["places"][0]["room"], "R0")
		room = document["rooms"][0]
		self.assertEqual((room["id"], room["label"], room["description"], room["places"], room["linked_objects"]), ("R0", "office", "a desk area", 1, 2))
		self.assertEqual(room["num_objects"], 2, "the summary's own count is kept next to the count derived from edges")
		self.assertEqual(document["summaries"], 1)
		self.assertEqual(document["counts"]["OBJECTS"], 2)
		self.assertEqual(document["counts"]["AGENTS"], 1)
		self.assertEqual(document["embeddings"]["sentence"], "sentence-transformers/sentence-t5-xl")
		self.assertEqual(document["embeddings"]["objects"], 2)
		text = json.dumps(document, allow_nan=False)
		self.assertNotIn("PRIVATE CHAIN OF THOUGHT", text)
		self.assertNotIn("reasoning", text)
		self.assertNotIn("0.1, 0.1", text, "no embedding vectors")

	def test_a_raw_graph_has_no_rooms_and_unusual_values_are_dropped(self) -> None:
		value = graph_document(objects=(5,))
		value["nodes"][0]["attributes"]["position"] = [float("nan"), 0.0, 0.0]
		value["nodes"][0]["attributes"]["metadata"]["description"] = "x" * 50000
		document = normalize_graph(value)
		self.assertEqual(document["rooms"], [])
		self.assertEqual(document["summaries"], 0)
		self.assertIsNone(document["objects"][0]["position"])
		self.assertLess(len(document["objects"][0]["description"]), 5000)
		self.assertIsNone(document["objects"][0]["room"])
		json.dumps(document, allow_nan=False)


class FakeStat:
	def __init__(self, base: os.stat_result, **overrides: int):
		for name in ("st_mode", "st_size", "st_mtime_ns", "st_ctime_ns", "st_ino", "st_dev", "st_mtime", "st_atime_ns"):
			setattr(self, name, overrides.get(name, getattr(base, name)))


class ArtifactMonitorTest(unittest.TestCase):
	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.root = Path(self.temp.name).resolve()
		self.registry = RunRegistry(output_root=self.root / "output", distro=DISTRO)
		self.events: List[Tuple[str, str, Dict[str, Any]]] = []
		self.monitor = ArtifactMonitor(self.registry, lambda run_id, kind, payload: self.events.append((run_id, kind, payload)), settle=0.0)

	def tearDown(self) -> None:
		self.monitor.stop()
		self.temp.cleanup()

	def open_result(self, name: str = "out_1") -> Tuple[Path, str]:
		result = write_minimal_bundle(self.root / "output" / name)
		return result, self.registry.open(str(result)).run_id

	def scan(self) -> List[Tuple[str, str, Dict[str, Any]]]:
		start = len(self.events)
		self.monitor.scan_once()
		return self.events[start:]

	def graph_events(self, events: List[Tuple[str, str, Dict[str, Any]]]) -> List[Tuple[str, str]]:
		return [(payload["reason"], payload["version"]["name"] if payload["version"] else None) for _, kind, payload in events if kind == "graph"]

	def test_initial_selection_promotion_and_update_each_publish_one_event(self) -> None:
		result, run_id = self.open_result()
		write_graph(result / "dsg.json", marker="raw")
		events = self.scan()
		self.assertEqual(self.graph_events(events), [("initial", "dsg.json")])
		self.assertEqual(events[0][0], run_id)
		self.assertEqual(self.scan(), [], "an unchanged result publishes nothing")
		self.assertEqual(self.monitor.active(run_id).document["objects"][0]["id"], "O0")
		write_graph(result / "clustered_dsg.json", marker="clustered", rooms=True)
		events = self.scan()
		self.assertEqual(self.graph_events(events), [("promoted", "clustered_dsg.json")])
		payload = events[0][2]
		self.assertEqual(payload["previous"]["name"], "dsg.json")
		self.assertEqual(payload["artifacts"]["clustered_dsg.json"]["state"], "valid")
		self.assertEqual(payload["artifacts"]["clustered_dsg_with_summaries.json"]["state"], "absent")
		self.assertEqual(payload["artifacts"]["dsg_updated.json"]["state"], "absent")
		self.assertEqual(payload["artifacts"]["dsg.json"]["state"], "valid")
		self.assertEqual(self.monitor.active(run_id).document["rooms"][0]["label"], "office")
		write_graph(result / "clustered_dsg.json", marker="clustered again", rooms=True)
		self.assertEqual(self.graph_events(self.scan()), [("updated", "clustered_dsg.json")])
		self.assertEqual(self.scan(), [])

	def test_deleting_the_active_graph_demotes_and_deleting_all_removes(self) -> None:
		result, run_id = self.open_result()
		write_graph(result / "dsg.json")
		write_graph(result / "clustered_dsg.json", marker="clustered")
		self.assertEqual(self.graph_events(self.scan()), [("initial", "clustered_dsg.json")])
		self.assertEqual(self.monitor.status(run_id)["artifacts"]["dsg.json"]["state"], "present", "lower candidates are not parsed")
		(result / "clustered_dsg.json").unlink()
		self.assertEqual(self.graph_events(self.scan()), [("demoted", "dsg.json")])
		(result / "dsg.json").unlink()
		events = self.scan()
		self.assertEqual(self.graph_events(events), [("removed", None)])
		self.assertEqual(events[0][2]["previous"]["name"], "dsg.json")
		self.assertIsNone(self.monitor.active(run_id))
		write_graph(result / "dsg_updated.json")
		self.assertEqual(self.graph_events(self.scan()), [("activated", "dsg_updated.json")])

	def test_a_result_without_a_graph_reports_none_once(self) -> None:
		self.open_result()
		self.assertEqual(self.graph_events(self.scan()), [("none", None)])
		self.assertEqual(self.scan(), [])

	def test_same_size_same_mtime_content_change_is_detected_by_digest(self) -> None:
		result, run_id = self.open_result()
		path = result / "dsg.json"
		write_graph(path, marker="aaaa")
		self.scan()
		before = os.stat(path)
		data = write_graph(path, marker="bbbb")
		os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
		after = os.stat(path)
		self.assertEqual((after.st_size, after.st_mtime_ns), (before.st_size, before.st_mtime_ns))
		events = self.scan()
		self.assertEqual(self.graph_events(events), [("updated", "dsg.json")])
		self.assertEqual(events[0][2]["version"]["digest"], hashlib.sha256(data).hexdigest())
		os.utime(path, ns=(after.st_atime_ns, after.st_mtime_ns + 5 * 10 ** 9))
		self.assertEqual(self.scan(), [], "a touch without new content is not a new version")
		self.assertEqual(self.monitor.active(run_id).version.mtime_ns, after.st_mtime_ns + 5 * 10 ** 9)

	def test_an_invalid_newer_candidate_is_reported_once_and_the_valid_graph_stays(self) -> None:
		result, run_id = self.open_result()
		write_graph(result / "clustered_dsg.json", marker="clustered")
		self.scan()
		(result / "clustered_dsg_with_summaries.json").write_text("{")
		events = self.scan()
		self.assertEqual(self.graph_events(events), [])
		errors = [payload for _, kind, payload in events if kind == "artifact-error"]
		self.assertEqual(len(errors), 1)
		self.assertEqual(errors[0]["name"], "clustered_dsg_with_summaries.json")
		self.assertEqual(errors[0]["active"]["name"], "clustered_dsg.json")
		self.assertEqual(self.scan(), [], "the same invalid version is reported once")
		self.assertEqual(self.monitor.status(run_id)["artifacts"]["clustered_dsg_with_summaries.json"]["state"], "invalid")
		write_graph(result / "clustered_dsg_with_summaries.json", marker="summaries", rooms=True)
		self.assertEqual(self.graph_events(self.scan()), [("promoted", "clustered_dsg_with_summaries.json")])

	def test_a_partial_rewrite_of_the_active_graph_keeps_its_last_valid_version(self) -> None:
		result, run_id = self.open_result()
		write_graph(result / "dsg.json", marker="raw")
		first = write_graph(result / "dsg_updated.json", marker="updated")
		self.scan()
		(result / "dsg_updated.json").write_text('{"nodes": [')
		events = self.scan()
		self.assertEqual(self.graph_events(events), [])
		self.assertEqual([kind for _, kind, _ in events], ["artifact-error"])
		active = self.monitor.active(run_id)
		self.assertEqual((active.name, active.version.digest), ("dsg_updated.json", hashlib.sha256(first).hexdigest()))
		write_graph(result / "dsg_updated.json", marker="updated twice")
		self.assertEqual(self.graph_events(self.scan()), [("updated", "dsg_updated.json")])

	def test_a_fresh_invalid_file_is_given_time_to_finish_before_it_is_reported(self) -> None:
		monitor = ArtifactMonitor(self.registry, lambda run_id, kind, payload: self.events.append((run_id, kind, payload)), settle=30.0)
		result, run_id = self.open_result()
		write_graph(result / "dsg.json")
		(result / "dsg_updated.json").write_text('{"nodes": [')
		monitor.scan_once()
		self.assertEqual([kind for _, kind, _ in self.events], ["graph"], "no error for a file that may still be written")
		self.assertEqual(self.events[0][2]["version"]["name"], "dsg.json")
		stat = os.stat(result / "dsg_updated.json")
		os.utime(result / "dsg_updated.json", ns=(stat.st_atime_ns, stat.st_mtime_ns - 60 * 10 ** 9))
		monitor.scan_once()
		self.assertEqual([kind for _, kind, _ in self.events], ["graph", "artifact-error"])

	def test_a_file_still_being_written_is_retried_without_an_error(self) -> None:
		result, run_id = self.open_result()
		write_graph(result / "dsg.json")
		real_stat = os.stat
		calls = {"n": 0}

		def moving_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
			calls["n"] += 1
			value = real_stat(path, *args, **kwargs)
			return FakeStat(value, st_mtime_ns=value.st_mtime_ns + calls["n"])

		with mock.patch.object(artifacts.os, "stat", side_effect=moving_stat):
			events = self.scan()
		self.assertEqual([kind for _, kind, _ in events], ["graph"])
		self.assertEqual(events[0][2]["reason"], "none")
		self.assertEqual(self.monitor.status(run_id)["artifacts"]["dsg.json"]["state"], "changing")
		self.assertEqual(self.graph_events(self.scan()), [("activated", "dsg.json")])

	def test_one_failing_run_does_not_stop_the_scan_of_another(self) -> None:
		bad, bad_id = self.open_result("out_bad")
		good, good_id = self.open_result("out_good")
		write_graph(bad / "dsg.json", marker="boom")
		write_graph(good / "dsg.json", marker="fine")
		real = artifacts.normalize_graph

		def explode(value: Dict[str, Any]) -> Dict[str, Any]:
			if value.get("marker") == "boom":
				raise RuntimeError("unexpected")
			return real(value)

		with mock.patch.object(artifacts, "normalize_graph", side_effect=explode), contextlib.redirect_stderr(io.StringIO()) as log:
			events = self.scan()
		self.assertEqual([(run_id, kind) for run_id, kind, _ in events], [(good_id, "graph")])
		self.assertIn("unexpected", log.getvalue())
		self.assertEqual([(run_id, payload["reason"]) for run_id, _, payload in self.scan()], [(bad_id, "initial")], "the failed run is retried")

	def test_a_failing_callback_does_not_stop_other_events(self) -> None:
		first, first_id = self.open_result("out_a")
		second, second_id = self.open_result("out_b")
		write_graph(first / "dsg.json")
		write_graph(second / "dsg.json")
		heard = []

		def callback(run_id: str, kind: str, payload: Dict[str, Any]) -> None:
			if run_id == first_id:
				raise RuntimeError("listener failed")
			heard.append(run_id)

		monitor = ArtifactMonitor(self.registry, callback, settle=0.0)
		with contextlib.redirect_stderr(io.StringIO()):
			monitor.scan_once()
		self.assertEqual(heard, [second_id])

	def test_closed_results_are_forgotten(self) -> None:
		result, run_id = self.open_result()
		write_graph(result / "dsg.json")
		self.scan()
		self.registry.close(run_id)
		self.assertEqual(self.scan(), [])
		self.assertIsNone(self.monitor.active(run_id))
		self.assertFalse(self.monitor.status(run_id)["scanned"])

	def test_the_thread_scans_a_newly_opened_result_promptly_and_stops(self) -> None:
		heard: "queue.Queue[Tuple[str, str, Dict[str, Any]]]" = queue.Queue()
		monitor = ArtifactMonitor(self.registry, lambda run_id, kind, payload: heard.put((run_id, kind, payload)), interval=60.0, settle=0.0)
		monitor.start()
		try:
			result = write_minimal_bundle(self.root / "output" / "out_thread")
			write_graph(result / "dsg.json")
			run_id = self.registry.open(str(result)).run_id
			event = heard.get(timeout=10)
			self.assertEqual((event[0], event[1], event[2]["version"]["name"]), (run_id, "graph", "dsg.json"))
			write_graph(result / "dsg_updated.json")
			monitor.request_scan()
			event = heard.get(timeout=10)
			self.assertEqual(event[2]["reason"], "promoted")
		finally:
			started = time.monotonic()
			monitor.stop()
			self.assertLess(time.monotonic() - started, 5)
		monitor.stop()


if __name__ == "__main__":
	unittest.main()
