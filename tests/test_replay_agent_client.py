"""The host-side client of the replay Agent worker, against the real worker module with a fake Agent (no Docker, no models)."""
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from scripts.replay_web import agent_client
from scripts.replay_web.agent_client import (
	AgentClient, AgentCrashed, AgentRequestFailed, AgentTimeout, AgentUnavailable, StagingError, default_argv, stage_file,
)
from scripts.replay_web.agent_protocol import RequestError, parse_request
from tests.replay_fixtures import graph_document

LAUNCHER = Path(__file__).resolve().parent / "replay_agent_launcher.py"
REPO = Path(__file__).resolve().parents[1]
PRIVATE = "PRIVATE-REASONING-DO-NOT-LEAK"
ARGUMENTS = "TOOL-ARGUMENTS-DO-NOT-LEAK"
LEAK = "sk-test-LEAK-KEY-1234567890"
KEY = "b" * 64
DIGEST = "a" * 64


def wait_until(condition, timeout=10.0):
	end = time.monotonic() + timeout
	while time.monotonic() < end:
		if condition():
			return True
		time.sleep(0.02)
	return False


class ClientTestCase(unittest.TestCase):
	def setUp(self):
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.repo = Path(self.directory.name)
		staged = self.repo / ".replay-web" / "cache" / KEY
		staged.mkdir(parents=True)
		document = graph_document()
		document["metadata"] = {"embedding_provenance": {
			"schema_version": 1,
			"sentence": {"model_name": "sentence-transformers/sentence-t5-xl", "backend": "sentence_transformers", "dim": 768, "pretrained": None},
			"clip": None,
		}}
		(staged / (DIGEST + ".json")).write_text(json.dumps(document), encoding="utf-8")
		self.spawned = []       # (argv, process) of every worker the client started
		self.log = []
		self.stopped = []
		self.removed = []
		self.marker = self.repo / "crash-marker"
		self.clients = []
		os.environ["FAKE_CRASH_MARKER"] = str(self.marker)
		self.addCleanup(os.environ.pop, "FAKE_CRASH_MARKER", None)

	def spawn(self, argv, **options):
		process = subprocess.Popen(argv, **options)
		self.spawned.append((argv, process))
		return process

	def client(self, **options):
		options.setdefault("argv", [sys.executable, str(LAUNCHER), "--repo-root", str(self.repo), "--idle-seconds", "0"])
		options.setdefault("models", ("gpt-5-mini",))
		options.setdefault("repo_root", REPO)
		options.setdefault("env", dict(os.environ, PYTHONUNBUFFERED="1"))   # the container sets it too: stderr lines arrive as they are written
		options.setdefault("spawn", self.spawn)
		options.setdefault("log", self.log.append)
		options.setdefault("stop_container", self.stopped.append)
		options.setdefault("remove_container", self.removed.append)
		client = AgentClient(**options)
		self.clients.append(client)
		self.addCleanup(client.stop)
		return client

	def request(self, question, id="turn-1", action="ask", **changes):
		request = {
			"id": id, "action": action, "result_key": KEY, "graph_path": ".replay-web/cache/%s/%s.json" % (KEY, DIGEST),
			"graph_version": "2-" + "c" * 20, "dataset": {"kind": "tum", "sequence": "fr1_desk", "time_origin": 0.0},
			"model": "gpt-5-mini", "question": question, "conversation": [], "context": {},
		}
		if action == "inspect":
			for name in ("model", "question", "conversation", "context"):
				del request[name]
		request.update(changes)
		return request

	def ask(self, client, question, id="turn-1", **kwargs):
		events = []
		result = client.ask(self.request(question, id), events.append, **kwargs)
		return result, events


class AskTest(ClientTestCase):
	def test_a_question_streams_progress_and_the_answer(self):
		client = self.client()

		result, events = self.ask(client, "echo: hello")

		kinds = [(event["type"], event.get("kind")) for event in events]
		self.assertEqual(kinds[:4], [("progress", "loading"), ("progress", "iteration"), ("progress", "tool"), ("progress", "final")])
		self.assertEqual(kinds[-1], ("delta", None))
		self.assertEqual("".join(event["text"] for event in events if event["type"] == "delta"), "echo: hello")
		self.assertEqual(result, {"id": "turn-1", "type": "complete", "answer": "echo: hello", "model": "gpt-5-mini", "graph_version": "2-" + "c" * 20})
		self.assertTrue(all(event["id"] == "turn-1" for event in events))

	def test_one_process_serves_many_questions(self):
		client = self.client()

		first, _ = self.ask(client, "pid", "t1")
		second, _ = self.ask(client, "pid", "t2")

		self.assertEqual(first["answer"], second["answer"])
		self.assertEqual(len(self.spawned), 1)
		self.assertEqual(str(self.spawned[0][1].pid), first["answer"])

	def test_the_caller_may_ignore_the_events(self):
		result = self.client().ask(self.request("echo: quiet"))

		self.assertEqual(result["answer"], "echo: quiet")

	def test_requests_queue_behind_the_running_one_in_order(self):
		client = self.client()
		finished, errors = [], []

		def run(name, question):
			try:
				client.ask(self.request(question, name), lambda event: None)
				finished.append(name)
			except Exception as error:
				errors.append(error)

		threads = []
		for name, question, expect_queued in (("t1", "slow 0.8", 0), ("t2", "echo: two", 1), ("t3", "echo: three", 2)):
			thread = threading.Thread(target=run, args=(name, question))
			thread.start()
			threads.append(thread)
			self.assertTrue(wait_until(lambda: client.busy and client.queued == expect_queued), (name, client.busy, client.queued))
		for thread in threads:
			thread.join(20)

		self.assertEqual((finished, errors), (["t1", "t2", "t3"], []))
		self.assertEqual(len(self.spawned), 1, "queued questions never start another worker")
		self.assertEqual((client.busy, client.queued), (False, 0))

	def test_a_failing_question_does_not_end_the_worker(self):
		client = self.client()
		before, _ = self.ask(client, "pid", "t0")

		with self.assertRaises(AgentRequestFailed) as caught:
			self.ask(client, "boom", "t1")
		after, _ = self.ask(client, "pid", "t2")

		self.assertEqual(caught.exception.code, "query-failed")
		self.assertNotIn(LEAK, caught.exception.message)
		self.assertEqual(before["answer"], after["answer"])
		self.assertEqual(len(self.spawned), 1)

	def test_a_request_that_is_not_valid_never_starts_a_worker(self):
		client = self.client()

		for bad in (self.request("q", model="gpt-5"), self.request("q", graph_path="/etc/passwd"), self.request(""), {"action": "shell"}):
			with self.assertRaises(RequestError):
				client.ask(bad, None)

		self.assertEqual(self.spawned, [])

	def test_a_request_object_is_accepted_as_well(self):
		request = parse_request(self.request("echo: object"), models=("gpt-5-mini",))

		self.assertEqual(self.client().ask(request)["answer"], "echo: object")


class FailureTest(ClientTestCase):
	def test_a_worker_that_dies_before_answering_is_started_again_once(self):
		client = self.client()

		result, _ = self.ask(client, "crash")

		self.assertEqual(result["answer"], "echo: recovered")
		self.assertEqual(len(self.spawned), 2)
		self.assertIsNotNone(self.spawned[0][1].poll())
		self.assertTrue(any("again" in line for line in self.log))

	def test_a_worker_that_keeps_dying_is_given_up_on_and_the_client_recovers(self):
		client = self.client()

		with self.assertRaises(AgentCrashed):
			self.ask(client, "crash-always")
		self.assertEqual(len(self.spawned), 2, "one retry, not a loop")
		result, _ = self.ask(client, "echo: back", "t2")

		self.assertEqual(result["answer"], "echo: back")
		self.assertEqual(len(self.spawned), 3)

	def test_a_crash_after_part_of_the_answer_is_not_retried(self):
		client = self.client()
		events = []

		with self.assertRaises(AgentCrashed):
			client.ask(self.request("crash-late"), events.append)

		self.assertEqual(len(self.spawned), 1)
		self.assertGreaterEqual(len([event for event in events if event["type"] == "delta"]), 1)
		self.assertEqual(self.ask(client, "echo: next", "t2")[0]["answer"], "echo: next")

	def test_a_silent_worker_times_out_and_is_replaced(self):
		client = self.client(idle_timeout=0.8)
		started = time.monotonic()

		with self.assertRaises(AgentTimeout):
			self.ask(client, "hang")

		self.assertLess(time.monotonic() - started, 10)
		self.assertTrue(wait_until(lambda: self.spawned[0][1].poll() is not None), "the stuck worker is killed")
		self.assertEqual(self.ask(client, "echo: fresh", "t2")[0]["answer"], "echo: fresh")
		self.assertEqual(len(self.spawned), 2)

	def test_the_whole_question_is_bounded_too(self):
		client = self.client(idle_timeout=30, request_timeout=1.0)

		with self.assertRaises(AgentTimeout):
			self.ask(client, "slow 5")

		self.assertTrue(wait_until(lambda: self.spawned[0][1].poll() is not None))

	def test_a_worker_that_cannot_start_is_unavailable_and_tried_again_later(self):
		failing = [True]

		def spawn(argv, **options):
			if failing[0]:
				raise FileNotFoundError(2, "No such file or directory", "docker")
			return self.spawn(argv, **options)

		client = self.client(spawn=spawn)

		with self.assertRaises(AgentUnavailable) as caught:
			self.ask(client, "echo: x")
		failing[0] = False

		self.assertNotIn(str(REPO), caught.exception.message)
		self.assertEqual(self.ask(client, "echo: ok", "t2")[0]["answer"], "echo: ok")

	def test_a_callback_that_raises_does_not_lose_the_answer(self):
		client = self.client()
		seen = []

		def broken(event):
			seen.append(event["type"])
			raise RuntimeError("the browser went away")

		result = client.ask(self.request("echo: still here"), broken)

		self.assertEqual(result["answer"], "echo: still here")
		self.assertGreater(len(seen), 3, "every event was still offered")
		self.assertEqual(len([line for line in self.log if "event callback" in line]), 1, "reported once")


class IsolationTest(ClientTestCase):
	def test_output_of_libraries_cannot_corrupt_the_stream_and_keys_are_masked_in_the_log(self):
		client = self.client()

		result, events = self.ask(client, "noise")

		self.assertEqual(result["answer"], "echo: noisy")
		self.assertTrue(wait_until(lambda: any("C-LEVEL NOISE" in line for line in self.log)), self.log)
		self.assertTrue(any("noise on stdout from a library" in line for line in self.log))
		logged = "\n".join(self.log)
		self.assertNotIn(LEAK, logged)
		self.assertIn("sk-***", logged)
		self.assertNotIn("NOISE", json.dumps(events))

	def test_nothing_private_reaches_the_caller(self):
		client = self.client()

		result, events = self.ask(client, "private")

		everything = json.dumps([result] + events)
		for private in (PRIVATE, ARGUMENTS, LEAK, "reasoning", "arguments"):
			self.assertNotIn(private, everything)
		self.assertEqual(result["answer"], "A plain answer.")

	def test_the_recent_worker_output_is_kept_for_diagnosis(self):
		client = self.client()
		self.ask(client, "noise")

		self.assertTrue(wait_until(lambda: any("noise on stdout" in line for line in client.recent_log())))
		self.assertNotIn(LEAK, "\n".join(client.recent_log()))


class LifecycleTest(ClientTestCase):
	def test_stopping_ends_the_worker_by_closing_its_input(self):
		client = self.client(stop_grace=10)
		self.ask(client, "echo: hi")
		process = self.spawned[0][1]
		started = time.monotonic()

		client.stop()

		self.assertIsNotNone(process.poll())
		self.assertLess(time.monotonic() - started, 8)
		self.assertEqual(self.stopped, [], "a worker that ends by itself needs no docker stop")
		with self.assertRaises(AgentUnavailable):
			self.ask(client, "echo: late", "t2")
		client.stop()   # a second stop is harmless

	def test_a_worker_that_ignores_end_of_input_is_stopped_by_name(self):
		client = self.client(stop_grace=0.5, container_name="daaam-replay-agent")
		outcome = []

		def run():
			try:
				self.ask(client, "hang")
			except Exception as error:
				outcome.append(error)

		thread = threading.Thread(target=run)
		thread.start()
		self.assertTrue(wait_until(lambda: client.busy and client.alive))

		client.stop()
		thread.join(15)

		self.assertEqual(self.stopped, ["daaam-replay-agent"])
		self.assertIsNotNone(self.spawned[0][1].poll())
		self.assertEqual([type(error) for error in outcome], [AgentUnavailable])

	def test_stopping_while_the_worker_starts_leaves_no_worker_behind(self):
		def slow_spawn(argv, **options):
			time.sleep(0.5)
			return self.spawn(argv, **options)

		client = self.client(spawn=slow_spawn, container_name="daaam-replay-agent")
		outcome = []

		def run():
			try:
				self.ask(client, "echo: hi")
			except Exception as error:
				outcome.append(type(error))

		thread = threading.Thread(target=run)
		thread.start()
		self.assertTrue(wait_until(lambda: client.busy))

		client.stop()   # the process does not exist yet
		thread.join(15)

		self.assertEqual(outcome, [AgentUnavailable])
		self.assertEqual(len(self.spawned), 1)
		self.assertTrue(wait_until(lambda: self.spawned[0][1].poll() is not None), "the worker that was started anyway is stopped")
		self.assertEqual(self.stopped, ["daaam-replay-agent"])

	def test_a_queued_question_is_released_when_the_client_stops(self):
		client = self.client(stop_grace=0.5)
		outcome = []

		def run(name, question):
			try:
				self.ask(client, question, name)
			except Exception as error:
				outcome.append((name, type(error)))

		first = threading.Thread(target=run, args=("t1", "hang"))
		first.start()
		self.assertTrue(wait_until(lambda: client.busy and client.alive))
		second = threading.Thread(target=run, args=("t2", "echo: queued"))
		second.start()
		self.assertTrue(wait_until(lambda: client.queued == 1))

		client.stop()
		first.join(15)
		second.join(15)

		self.assertEqual(sorted(outcome), [("t1", AgentUnavailable), ("t2", AgentUnavailable)])

	def test_an_idle_worker_can_be_released_but_a_busy_one_is_not(self):
		client = self.client(stop_grace=10)
		self.assertFalse(client.release_if_idle(), "nothing runs: nothing to release")
		self.ask(client, "echo: hi")
		process = self.spawned[0][1]

		self.assertTrue(client.release_if_idle())
		self.assertIsNotNone(process.poll())
		self.assertEqual(self.ask(client, "echo: again", "t2")[0]["answer"], "echo: again")
		self.assertEqual(len(self.spawned), 2, "the next question starts a new worker")

		running = threading.Thread(target=lambda: self.ask(client, "slow 1.0", "t3"))
		running.start()
		self.assertTrue(wait_until(lambda: client.busy))
		self.assertFalse(client.release_if_idle())
		running.join(15)
		self.assertTrue(client.alive)

	def test_invalidate_needs_no_worker_and_is_acknowledged_by_a_running_one(self):
		client = self.client()

		self.assertFalse(client.invalidate(KEY), "no worker, nothing cached")
		self.assertEqual(self.spawned, [])
		self.ask(client, "echo: hi")
		self.assertTrue(client.invalidate(KEY))

		self.assertEqual(self.ask(client, "invalidated", "t2")[0]["answer"], KEY)
		self.assertEqual(len(self.spawned), 1)

	def test_inspect_reports_the_capability_of_a_staged_graph(self):
		client = self.client()

		capability = client.inspect(self.request("", action="inspect"))

		self.assertEqual((capability["ready"], capability["reason"]), (True, None))
		self.assertEqual(capability["provenance"]["sentence"]["model_name"], "sentence-transformers/sentence-t5-xl")

	def test_a_stale_container_of_the_fixed_name_is_removed_before_each_start(self):
		client = self.client(container_name="daaam-replay-agent")

		self.ask(client, "crash")   # the first worker dies, a second one is started

		self.assertEqual(self.removed, ["daaam-replay-agent", "daaam-replay-agent"])

	def test_a_custom_command_has_no_container_to_remove(self):
		self.ask(self.client(), "echo: hi")

		self.assertEqual(self.removed, [])


class WorkerProcessTest(ClientTestCase):
	"""The worker as a process of its own: how it ends."""

	def start(self, *extra):
		return subprocess.Popen(
			[sys.executable, str(LAUNCHER), "--repo-root", str(self.repo)] + list(extra),
			stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(REPO),
			env=dict(os.environ, PYTHONUNBUFFERED="1"),
		)

	def test_an_idle_worker_ends_cleanly_while_its_input_is_still_open(self):
		process = self.start("--idle-seconds", "0.5")
		self.addCleanup(process.kill)
		self.assertEqual(json.loads(process.stdout.readline())["type"], "ready")

		code = process.wait(timeout=20)
		error = process.stderr.read().decode("utf-8", "replace")

		self.assertEqual(code, 0, error)
		self.assertNotIn("Fatal Python error", error)
		self.assertIn("idle for 0.5 seconds", error)

	def test_end_of_input_ends_the_worker_cleanly(self):
		process = self.start("--idle-seconds", "0")
		self.addCleanup(process.kill)
		self.assertEqual(json.loads(process.stdout.readline())["type"], "ready")

		process.stdin.close()
		code = process.wait(timeout=20)

		self.assertEqual((code, process.stdout.read()), (0, b""))
		self.assertNotIn("Fatal Python error", process.stderr.read().decode("utf-8", "replace"))


class CommandTest(unittest.TestCase):
	def test_the_worker_command_is_fixed(self):
		self.assertEqual(default_argv(("gpt-5-mini", "claude-sonnet-4-5")), [
			"docker", "compose", "run", "--rm", "-T", "--name", "daaam-replay-agent",
			"-e", "HF_HOME=/data/hf_cache", "-e", "PYTHONUNBUFFERED=1", "-e", "REPLAY_AGENT_MODELS=gpt-5-mini,claude-sonnet-4-5",
			"daaam-ws", "python", "-m", "scripts.replay_web.agent_worker",
		])

	def test_the_container_name_stays_in_the_workspaces_own_namespace(self):
		self.assertEqual(agent_client.AGENT_CONTAINER, "daaam-replay-agent")
		self.assertTrue(agent_client.AGENT_CONTAINER.startswith("daaam-replay-"), "docker_busy_reason does not count it as another pipeline")

	def test_only_the_fixed_name_is_ever_given_to_docker(self):
		calls = []
		agent_client.docker_stop_named("daaam-replay-agent", run=lambda argv, **options: calls.append(argv))
		agent_client.docker_remove_named("daaam-replay-agent", run=lambda argv, **options: calls.append(argv))
		agent_client.docker_stop_named("other-container; rm -rf /", run=lambda argv, **options: calls.append(argv))
		agent_client.docker_remove_named("../x", run=lambda argv, **options: calls.append(argv))

		self.assertEqual(calls, [["docker", "stop", "-t", "10", "daaam-replay-agent"], ["docker", "rm", "-f", "daaam-replay-agent"]])


class StagingTest(unittest.TestCase):
	def setUp(self):
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.repo = Path(self.directory.name) / "repo"
		self.repo.mkdir()
		self.results = Path(self.directory.name) / "results"
		self.results.mkdir()

	def source(self, content=b'{"graph": 1}', name="dsg_updated.json"):
		path = self.results / name
		path.write_bytes(content)
		return path, hashlib.sha256(content).hexdigest()

	def cache(self, key=KEY):
		return self.repo / ".replay-web" / "cache" / key

	def test_a_file_is_staged_under_its_digest_and_the_path_is_accepted_by_the_protocol(self):
		path, digest = self.source()

		relative = stage_file(self.repo, KEY, path, digest)

		self.assertEqual(relative, ".replay-web/cache/%s/%s.json" % (KEY, digest))
		self.assertEqual((self.repo / relative).read_bytes(), b'{"graph": 1}')
		request = parse_request({
			"id": "t", "action": "inspect", "result_key": KEY, "graph_path": relative, "graph_version": "1-x",
			"dataset": {"kind": "tum", "sequence": "s", "time_origin": 0.0},
		})
		self.assertEqual(request.graph_path, relative)

	def test_the_staged_files_are_private(self):
		path, digest = self.source()
		relative = stage_file(self.repo, KEY, path, digest)

		self.assertEqual(stat.S_IMODE((self.repo / relative).stat().st_mode), 0o600)
		self.assertEqual(stat.S_IMODE(self.cache().stat().st_mode), 0o700)

	def test_a_source_that_changed_since_it_was_validated_is_refused_and_leaves_nothing(self):
		path, digest = self.source()
		path.write_bytes(b'{"graph": 2}')   # a hot reload replaced it after the digest was taken

		with self.assertRaises(StagingError):
			stage_file(self.repo, KEY, path, digest)

		self.assertEqual(list(self.cache().iterdir()) if self.cache().exists() else [], [])

	def test_staging_a_known_version_again_reuses_the_copy(self):
		path, digest = self.source()
		relative = stage_file(self.repo, KEY, path, digest)
		staged = self.repo / relative
		os.utime(str(staged), (1000, 1000))
		path.unlink()   # the source is gone: the snapshot still serves

		again = stage_file(self.repo, KEY, path, digest)

		self.assertEqual(again, relative)
		self.assertGreater(staged.stat().st_mtime, 1000, "reuse marks it as recently used")
		self.assertEqual(sorted(entry.name for entry in self.cache().iterdir()), [digest + ".json"])

	def test_a_background_file_has_its_own_suffix(self):
		path, digest = self.source(b"objects: []\n", "background_objects.yaml")

		relative = stage_file(self.repo, KEY, path, digest, suffix=".background.yaml")

		self.assertEqual(relative, ".replay-web/cache/%s/%s.background.yaml" % (KEY, digest))

	def test_only_the_newest_versions_of_a_result_are_kept(self):
		digests = []
		for number in range(4):
			path, digest = self.source(b'{"graph": %d}' % number)
			stage_file(self.repo, KEY, path, digest, keep=2)
			os.utime(str(self.cache() / (digest + ".json")), (1000 + number, 1000 + number))
			digests.append(digest)

		self.assertEqual(sorted(entry.name for entry in self.cache().iterdir()), sorted(digest + ".json" for digest in digests[-2:]))

	def test_graphs_and_background_files_are_kept_in_separate_rounds(self):
		yaml, yaml_digest = self.source(b"objects: []\n", "background_objects.yaml")
		stage_file(self.repo, KEY, yaml, yaml_digest, suffix=".background.yaml")
		for number in range(3):
			graph, digest = self.source(b'{"graph": %d}' % number)
			stage_file(self.repo, KEY, graph, digest, keep=2)
			os.utime(str(self.cache() / (digest + ".json")), (1000 + number, 1000 + number))

		names = sorted(entry.name for entry in self.cache().iterdir())
		self.assertEqual(len([name for name in names if name.endswith(".json")]), 2)
		self.assertIn(yaml_digest + ".background.yaml", names, "new graphs do not push the background file out")

	def test_the_whole_cache_is_bounded_and_the_current_version_stays(self):
		kept = None
		for number, key in enumerate(("1" * 64, "2" * 64, "3" * 64)):
			path, digest = self.source(b"x" * 1000 + b"%d" % number)
			kept = stage_file(self.repo, key, path, digest, max_bytes=2500)
			os.utime(str(self.repo / kept), (1000 + number, 1000 + number))

		remaining = sorted(path.parent.name for path in (self.repo / ".replay-web" / "cache").glob("*/*"))
		self.assertEqual(remaining, ["2" * 64, "3" * 64], "the oldest result's snapshot went first")
		self.assertTrue((self.repo / kept).exists())

	def test_crash_leftovers_are_cleaned_up(self):
		path, digest = self.source()
		stale = self.cache() / ".tmp-1-abcd"
		self.cache().mkdir(parents=True)
		stale.write_bytes(b"half")
		os.utime(str(stale), (1000, 1000))

		stage_file(self.repo, KEY, path, digest)

		self.assertFalse(stale.exists())

	def test_a_linked_cache_directory_is_refused(self):
		path, digest = self.source()
		outside = Path(self.directory.name) / "outside"
		outside.mkdir()
		(self.repo / ".replay-web" / "cache").mkdir(parents=True)
		self.cache().symlink_to(outside, target_is_directory=True)

		with self.assertRaises(StagingError):
			stage_file(self.repo, KEY, path, digest)

		self.assertEqual(list(outside.iterdir()), [])

	def test_a_key_or_digest_that_is_not_plain_is_refused(self):
		path, digest = self.source()
		for key, name in (("../x", digest), ("a/b", digest), ("", digest), (KEY, "../" + digest), (KEY, "xyz"), (KEY, digest[:-1])):
			with self.subTest(key=key, digest=name):
				with self.assertRaises(StagingError):
					stage_file(self.repo, key, path, name)


if __name__ == "__main__":
	unittest.main()
