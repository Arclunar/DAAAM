import json
import queue
import subprocess
import tempfile
import threading
import time
import unittest
from itertools import count
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from scripts.replay_web.events import EventBroker
from scripts.replay_web.jobs import (
	JobConflict, JobManager, JobNotFound, JobRecord, JobRefused, JobSpec, JobStage, JobStateError, build_job_argv,
	docker_busy_reason, docker_container_alive, has_openai_key, recover_leftovers,
)

PIDS = count(1000)


class FakeProcess:
	"""What the job manager needs of a child: output lines, an exit status and group signals. Nothing runs."""

	def __init__(self, ignore_term: bool = False, ignore_stop: bool = False):
		self.pid = next(PIDS)
		self.returncode: Optional[int] = None
		self.signals: List[str] = []
		self.ignore_term = ignore_term
		self.ignore_stop = ignore_stop
		self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
		self._done = threading.Event()
		self.stdout = self._read()

	def _read(self):
		while True:
			line = self._lines.get()
			if line is None:
				return
			yield line

	def emit(self, line: str) -> None:
		self._lines.put(line + "\n")

	def finish(self, code: int = 0) -> None:
		if self.returncode is None:
			self.returncode = code
			self._lines.put(None)
			self._done.set()

	def wait(self, timeout: Optional[float] = None) -> int:
		if not self._done.wait(timeout):
			raise subprocess.TimeoutExpired("fake", timeout)
		return self.returncode

	def poll(self) -> Optional[int]:
		return self.returncode

	def terminate_group(self) -> None:
		self.signals.append("TERM")
		if not self.ignore_term:
			self.finish(143)

	def kill_group(self) -> None:
		self.signals.append("KILL")
		self.finish(137)


class FakeRunner:
	def __init__(self) -> None:
		self.calls: List[Dict[str, Any]] = []
		self.processes: List[FakeProcess] = []
		self.error: Optional[Exception] = None
		self.options: Dict[str, Any] = {}

	def __call__(self, argv: List[str], env: Dict[str, str], cwd: Path) -> FakeProcess:
		self.calls.append({"argv": list(argv), "env": dict(env), "cwd": str(cwd)})
		if self.error is not None:
			raise self.error
		process = FakeProcess(**self.options)
		self.processes.append(process)
		return process


class JobTestCase(unittest.TestCase):
	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.root = Path(self.temp.name).resolve()
		self.state = self.root / "state"
		self.repo = self.root / "repo"
		self.repo.mkdir()
		self.broker = EventBroker(history_limit=2000, queue_limit=5000)
		self.runner = FakeRunner()
		self.stopped: List[str] = []   # `docker stop` calls: the job IDs
		self.stop_effect = True   # whether `docker stop` ends the container and with it the docker client
		self.alive: set = set()   # containers that still run after their client ended
		self.finished: List[JobRecord] = []
		self.reports = True   # the default owner callback records finished jobs; a test with its own callback clears this
		self.subscription = self.broker.subscribe(["a", "b", "c"], after_id=0)
		self.managers: List[JobManager] = []

	def tearDown(self) -> None:
		for manager in self.managers:
			manager.shutdown()
		self.subscription.close()
		self.temp.cleanup()

	def manager(self, **options: Any) -> JobManager:
		settings: Dict[str, Any] = dict(
			state_root=self.state, broker=self.broker, runner=self.runner, repo_root=self.repo, max_concurrent=1,
			api_key_check=lambda: True, busy_check=lambda: None, stop_container=self.docker_stop,
			container_alive=lambda job_id: job_id in self.alive, retry_interval=0.02,
			on_finished=self.finished.append, grace=0.05, stop_wait=0.05, log_limit=4096,
		)
		settings.update(options)
		manager = JobManager(**settings)
		self.managers.append(manager)
		return manager

	def docker_stop(self, job_id: str) -> None:
		"""The fake `docker stop`: the container ends, and so does the docker client that was attached to it."""
		self.stopped.append(job_id)
		self.alive.discard(job_id)
		if self.stop_effect:
			for call, process in zip(list(self.runner.calls), list(self.runner.processes)):
				if "daaam-replay-job-" + job_id in call["argv"]:
					process.finish(143)

	def result(self, name: str, *files: str) -> Path:
		path = self.root / "output" / name
		path.mkdir(parents=True, exist_ok=True)
		for file in ("dsg.json",) + files:
			(path / file).write_text("{}", encoding="utf-8")
		return path

	def spec(self, path: Path, run_id: str = "a", stage: JobStage = JobStage.POSTPROCESS, kind: str = "tum", replace: bool = False) -> JobSpec:
		return JobSpec(run_id=run_id, result_path=path, dataset_kind=kind, stage=stage, replace=replace)

	def wait_for(self, predicate: Callable[[], Any], timeout: float = 10.0, message: str = "condition") -> Any:
		deadline = time.monotonic() + timeout
		while True:
			value = predicate()
			if value:
				return value
			if time.monotonic() > deadline:
				self.fail(f"timed out waiting for {message}")
			time.sleep(0.01)

	def wait_state(self, manager: JobManager, job_id: str, state: str) -> JobRecord:
		"""Wait for a state. A final state is only awaited as reported: saved, published and handed to the owner."""
		record = self.wait_for(lambda: (lambda record: record if record.state == state else None)(manager.get(job_id)), message=f"job {job_id} to be {state}")
		if self.reports and state in ("succeeded", "failed", "cancelled", "interrupted"):
			self.wait_for(lambda: any(item.job_id == job_id for item in self.finished), message=f"job {job_id} to be reported finished")
		return record

	def events(self, event_type: Optional[str] = None) -> List[Any]:
		found = []
		while True:
			try:
				event = self.subscription.get(timeout=0.02)
			except queue.Empty:
				return found
			if event_type is None or event.event_type == event_type:
				found.append(event)

	def finish_with_files(self, process: FakeProcess, path: Path, *files: str) -> None:
		for file in files:
			(path / file).write_text("{}", encoding="utf-8")
		process.finish(0)


class JobLifecycleTest(JobTestCase):
	def test_a_job_runs_to_success_and_publishes_every_state(self) -> None:
		path = self.result("tum")
		manager = self.manager()
		record = manager.start(self.spec(path))
		self.assertIn(record.state, ("queued", "running"))
		running = self.wait_state(manager, record.job_id, "running")
		self.assertIsNotNone(running.started)
		process = self.runner.processes[0]
		process.emit("=== sentence embeddings")
		self.finish_with_files(process, path, "dsg_updated.json", "clustered_dsg.json")
		done = self.wait_state(manager, record.job_id, "succeeded")
		self.assertEqual((done.exit_code, done.message), (0, None))
		self.assertGreaterEqual(done.finished, done.started)
		states = [event.payload["job"]["state"] for event in self.events("job-state")]
		self.assertEqual(states, ["queued", "running", "succeeded"])
		self.assertEqual([job.state for job in self.finished], ["succeeded"], "the owner is told once, after the final state")

	def test_one_mutating_job_per_result(self) -> None:
		path = self.result("tum")
		manager = self.manager()
		first = manager.start(self.spec(path))
		with self.assertRaises(JobConflict):
			manager.start(self.spec(path, stage=JobStage.SUMMARIZE))
		with self.assertRaises(JobConflict):
			manager.start(self.spec(path, run_id="b"))   # the same result under another run id is the same result
		self.assertEqual(manager.get(first.job_id).state in ("queued", "running"), True)
		self.wait_state(manager, first.job_id, "running")
		self.runner.processes[0].finish(1)
		self.wait_state(manager, first.job_id, "failed")
		again = manager.start(self.spec(path))   # a finished job no longer blocks
		self.assertNotEqual(again.job_id, first.job_id)

	def test_global_concurrency_queues_jobs_in_order(self) -> None:
		a, b, c = self.result("a"), self.result("b"), self.result("c")
		manager = self.manager(max_concurrent=1)
		first = manager.start(self.spec(a, run_id="a"))
		second = manager.start(self.spec(b, run_id="b"))
		third = manager.start(self.spec(c, run_id="c"))
		self.wait_state(manager, first.job_id, "running")
		self.assertEqual([manager.get(second.job_id).state, manager.get(third.job_id).state], ["queued", "queued"])
		self.assertEqual(len(self.runner.calls), 1)
		self.finish_with_files(self.runner.processes[0], a, "dsg_updated.json", "clustered_dsg.json")
		self.wait_state(manager, second.job_id, "running")
		self.assertEqual(manager.get(third.job_id).state, "queued")
		self.finish_with_files(self.runner.processes[1], b, "dsg_updated.json", "clustered_dsg.json")
		self.wait_state(manager, third.job_id, "running")
		self.assertEqual(len(self.runner.calls), 3)

	def test_a_higher_limit_runs_different_results_together(self) -> None:
		a, b = self.result("a"), self.result("b")
		manager = self.manager(max_concurrent=2)
		first, second = manager.start(self.spec(a, run_id="a")), manager.start(self.spec(b, run_id="b"))
		self.wait_state(manager, first.job_id, "running")
		self.wait_state(manager, second.job_id, "running")

	def test_success_needs_exit_zero_and_the_expected_files(self) -> None:
		path = self.result("tum")
		manager = self.manager()
		job = manager.start(self.spec(path, stage=JobStage.ALL))
		self.wait_state(manager, job.job_id, "running")
		self.finish_with_files(self.runner.processes[0], path, "dsg_updated.json", "clustered_dsg.json")   # no summaries written
		failed = self.wait_state(manager, job.job_id, "failed")
		self.assertEqual(failed.exit_code, 0)
		self.assertIn("clustered_dsg_with_summaries.json", failed.message)
		self.assertIn("region_summaries.yaml", failed.message)
		job = manager.start(self.spec(path, stage=JobStage.POSTPROCESS, replace=True))
		self.wait_state(manager, job.job_id, "running")
		self.runner.processes[1].finish(3)
		failed = self.wait_state(manager, job.job_id, "failed")
		self.assertEqual((failed.exit_code, failed.message), (3, "exit status 3"))

	def test_files_left_over_from_an_older_run_do_not_make_a_job_succeed(self) -> None:
		path = self.result("tum", "dsg_updated.json", "clustered_dsg.json")
		old = time.time() - 3600
		for file in ("dsg_updated.json", "clustered_dsg.json"):
			import os
			os.utime(str(path / file), (old, old))
		manager = self.manager()
		job = manager.start(self.spec(path, replace=True))
		self.wait_state(manager, job.job_id, "running")
		self.runner.processes[0].finish(0)
		failed = self.wait_state(manager, job.job_id, "failed")
		self.assertIn("did not write", failed.message)

	def test_a_child_that_cannot_start_fails_the_job(self) -> None:
		self.runner.error = FileNotFoundError(2, "No such file or directory", "docker")
		path = self.result("tum")
		manager = self.manager()
		job = manager.start(self.spec(path))
		failed = self.wait_state(manager, job.job_id, "failed")
		self.assertIn("docker", failed.message)
		self.assertIsNone(failed.exit_code)
		self.assertEqual([job.state for job in self.finished], ["failed"])

	def test_a_killed_child_is_described(self) -> None:
		path = self.result("tum")
		manager = self.manager()
		job = manager.start(self.spec(path))
		self.wait_state(manager, job.job_id, "running")
		self.runner.processes[0].finish(137)
		failed = self.wait_state(manager, job.job_id, "failed")
		self.assertIn("137", failed.message)
		self.assertIn("memory", failed.message)

	def test_the_owner_hears_about_failures_too_and_a_failing_owner_breaks_nothing(self) -> None:
		def explode(record: JobRecord) -> None:
			raise RuntimeError("owner failed")

		self.reports = False
		path = self.result("tum")
		manager = self.manager(on_finished=explode)
		job = manager.start(self.spec(path))
		self.wait_state(manager, job.job_id, "running")
		self.runner.processes[0].finish(1)
		self.wait_state(manager, job.job_id, "failed")
		next_job = manager.start(self.spec(path))
		self.assertEqual(manager.get(next_job.job_id).state in ("queued", "running"), True)


class JobPreconditionTest(JobTestCase):
	def test_rerunning_existing_outputs_needs_an_explicit_replace(self) -> None:
		path = self.result("tum", "dsg_updated.json", "clustered_dsg.json")
		manager = self.manager()
		with self.assertRaises(JobRefused) as caught:
			manager.start(self.spec(path))
		self.assertEqual(caught.exception.code, "confirm")
		self.assertIn("clustered_dsg.json", str(caught.exception))
		self.assertEqual(self.runner.calls, [])
		self.assertIsNotNone(manager.start(self.spec(path, replace=True)))

	def test_each_stage_checks_only_its_own_outputs(self) -> None:
		path = self.result("tum", "dsg_updated.json", "clustered_dsg.json")
		manager = self.manager()
		job = manager.start(self.spec(path, stage=JobStage.SUMMARIZE))   # summaries do not exist yet: no confirmation needed
		self.wait_state(manager, job.job_id, "running")

	def test_summarize_needs_the_clustered_graph_and_every_stage_needs_dsg_json(self) -> None:
		manager = self.manager()
		raw = self.result("raw")
		with self.assertRaises(JobRefused) as caught:
			manager.start(self.spec(raw, stage=JobStage.SUMMARIZE))
		self.assertEqual(caught.exception.code, "prerequisite")
		self.assertIn("Postprocess", str(caught.exception))
		empty = self.root / "output" / "empty"
		empty.mkdir(parents=True)
		for stage in JobStage:
			with self.subTest(stage=stage), self.assertRaises(JobRefused) as caught:
				manager.start(self.spec(empty, stage=stage))
			self.assertEqual(caught.exception.code, "prerequisite")
		with self.assertRaises(JobRefused) as caught:
			manager.start(self.spec(self.root / "missing"))
		self.assertEqual(caught.exception.code, "prerequisite")

	def test_summaries_need_an_api_key_but_postprocessing_does_not(self) -> None:
		path = self.result("tum", "clustered_dsg.json")
		manager = self.manager(api_key_check=lambda: False)
		for stage in (JobStage.SUMMARIZE, JobStage.ALL):
			with self.subTest(stage=stage), self.assertRaises(JobRefused) as caught:
				manager.start(self.spec(path, stage=stage, replace=True))
			self.assertEqual(caught.exception.code, "api-key")
			self.assertIn("OPENAI_API_KEY", str(caught.exception))
		self.assertIsNotNone(manager.start(self.spec(path, stage=JobStage.POSTPROCESS, replace=True)))

	def test_another_heavy_container_blocks_a_start_and_a_queued_launch(self) -> None:
		a, b = self.result("a"), self.result("b")
		busy = {"reason": "a pipeline is running"}
		manager = self.manager(busy_check=lambda: busy["reason"])
		with self.assertRaises(JobRefused) as caught:
			manager.start(self.spec(a))
		self.assertEqual(caught.exception.code, "busy")
		self.assertIn("a pipeline is running", str(caught.exception))
		busy["reason"] = None
		first = manager.start(self.spec(a, run_id="a"))
		second = manager.start(self.spec(b, run_id="b"))
		self.wait_state(manager, first.job_id, "running")
		busy["reason"] = "pipeline started meanwhile"
		self.finish_with_files(self.runner.processes[0], a, "dsg_updated.json", "clustered_dsg.json")
		failed = self.wait_state(manager, second.job_id, "failed")
		self.assertIn("pipeline started meanwhile", failed.message)
		self.assertEqual(len(self.runner.calls), 1, "the blocked job never reached docker")

	def test_a_result_path_that_cannot_be_mounted_safely_is_refused(self) -> None:
		manager = self.manager()
		for bad in ("relative/path", "output/tum", "/tmp/a:b", "/tmp/a\nb", "/tmp/a\x00b", ""):
			with self.subTest(path=bad), self.assertRaises(JobRefused) as caught:
				manager.start(self.spec(Path(bad)))
			self.assertEqual(caught.exception.code, "path")
		self.assertEqual(self.runner.calls, [])

	def test_nothing_starts_after_the_shutdown(self) -> None:
		path = self.result("tum")
		manager = self.manager()
		manager.shutdown()
		with self.assertRaises(JobRefused) as caught:
			manager.start(self.spec(path))
		self.assertEqual(caught.exception.code, "shutdown")


class JobCommandTest(JobTestCase):
	def test_tum_and_coda_commands_differ_only_in_their_dataset_settings(self) -> None:
		tum = Path("/data/output/tum_replay/out_1")
		argv, env = build_job_argv(self.spec(tum, kind="tum", stage=JobStage.POSTPROCESS), "0123456789abcdef", self.repo)
		self.assertEqual(argv, [
			"docker", "compose", "run", "--rm", "-T",
			"--name", "daaam-replay-job-0123456789abcdef",
			"-v", "/data/output/tum_replay/out_1:/selected-result",
			"-e", "PYTHONUNBUFFERED=1",
			"-e", "CLUSTER_PLACES_CONFIG=config/cluster_places_config_tum.yaml",
			"-e", "PLACES_LAYER=PLACES",
			"daaam-ws", "bash", "scripts/postprocess_coda.sh", "--atomic", "--stage", "postprocess", "/selected-result",
		])
		coda = Path("/mnt/e/output/coda/out_2")
		argv, _ = build_job_argv(self.spec(coda, kind="coda", stage=JobStage.ALL), "fedcba9876543210", self.repo)
		self.assertEqual(argv, [
			"docker", "compose", "run", "--rm", "-T",
			"--name", "daaam-replay-job-fedcba9876543210",
			"-v", "/mnt/e/output/coda/out_2:/selected-result",
			"-e", "PYTHONUNBUFFERED=1",
			"daaam-ws", "bash", "scripts/postprocess_coda.sh", "--atomic", "--stage", "all", "/selected-result",
		])
		self.assertTrue(all(isinstance(item, str) for item in argv))
		self.assertEqual(env, {"COMPOSE_ANSI": "never", "COMPOSE_PROGRESS": "plain"})

	def test_the_manager_runs_exactly_that_command_in_the_repository(self) -> None:
		path = self.result("tum")
		manager = self.manager()
		job = manager.start(self.spec(path, stage=JobStage.POSTPROCESS))
		self.wait_state(manager, job.job_id, "running")
		call = self.runner.calls[0]
		self.assertEqual(call["argv"], build_job_argv(self.spec(path), job.job_id, self.repo)[0])
		self.assertEqual(call["cwd"], str(self.repo))
		self.assertEqual(call["env"]["COMPOSE_ANSI"], "never")
		self.assertEqual(call["argv"][:3], ["docker", "compose", "run"], "docker is executed directly, not through a shell")

	def test_browser_text_only_ever_becomes_one_argument(self) -> None:
		hostile = Path("/data/output/$(touch pwned) ; rm -rf x/'out`1")
		argv, _ = build_job_argv(self.spec(hostile, kind="coda"), "0123456789abcdef", self.repo)
		self.assertEqual(argv[argv.index("-v") + 1], "%s:/selected-result" % hostile)
		self.assertEqual(argv.count("/selected-result"), 1)
		self.assertNotIn("-c", argv)
		self.assertEqual(argv[:3], ["docker", "compose", "run"])
		for stage in ("postprocess; id", "ALL", "", "--atomic"):
			with self.subTest(stage=stage), self.assertRaises(ValueError):
				JobStage(stage)

	def test_the_docker_guard_ignores_the_workspace_jobs_and_finds_other_pipelines(self) -> None:
		def fake_ps(output: str) -> Callable[..., Any]:
			return lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

		self.assertIsNone(docker_busy_reason(fake_ps("")))
		self.assertIsNone(docker_busy_reason(fake_ps("daaam-replay-job-0123\tbash scripts/postprocess_coda.sh --atomic\n")))
		self.assertIsNone(docker_busy_reason(fake_ps("web\tnginx -g daemon off\n")))
		reason = docker_busy_reason(fake_ps("nice_wu\tbash scripts/run_coda_example.sh\n"))
		self.assertIn("run_coda_example", reason)
		self.assertIn("docker", docker_busy_reason(lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, stdout="", stderr="daemon down")).lower())

		def missing(argv: List[str], **kwargs: Any) -> Any:
			raise FileNotFoundError("docker")

		self.assertIn("docker", docker_busy_reason(missing).lower())

	def test_the_api_key_is_found_in_the_environment_or_the_env_file_without_reading_its_value_aloud(self) -> None:
		self.assertFalse(has_openai_key(self.repo, {}))
		self.assertTrue(has_openai_key(self.repo, {"OPENAI_API_KEY": "sk-x"}))
		self.assertFalse(has_openai_key(self.repo, {"OPENAI_API_KEY": ""}))
		(self.repo / ".env").write_text("OTHER=1\nOPENAI_API_KEY=\n", encoding="utf-8")
		self.assertFalse(has_openai_key(self.repo, {}))
		(self.repo / ".env").write_text("OTHER=1\nOPENAI_API_KEY=sk-secret\n", encoding="utf-8")
		self.assertTrue(has_openai_key(self.repo, {}))


class JobLogTest(JobTestCase):
	def test_output_is_streamed_in_batches_and_bounded_on_disk(self) -> None:
		path = self.result("tum")
		manager = self.manager(log_limit=2048)
		job = manager.start(self.spec(path))
		self.wait_state(manager, job.job_id, "running")
		process = self.runner.processes[0]
		for number in range(3000):
			process.emit("line %04d %s" % (number, "x" * 40))
		process.emit("y" * 100000)   # one enormous line
		self.finish_with_files(process, path, "dsg_updated.json", "clustered_dsg.json")
		self.wait_state(manager, job.job_id, "succeeded")
		log = self.state / "jobs" / job.job_id / "log.txt"
		self.assertLessEqual(log.stat().st_size, 2 * 2048 + 8192, "the log file stays bounded")
		tail = manager.log_tail(job.job_id, 5)
		self.assertEqual(len(tail), 5)
		self.assertTrue(tail[-1].endswith("…"), "a huge line is cut")
		self.assertLess(len(tail[-1]), 5000)
		self.assertIn("line 2999", tail[-2])
		batches = self.events("job-log")
		self.assertTrue(batches)
		self.assertLess(len(batches), 3001, "lines are batched, not one event each")
		streamed = [line for event in batches for line in event.payload["lines"]]
		self.assertIn("line 0000 %s" % ("x" * 40), streamed[0])
		self.assertEqual(batches[0].payload["job_id"], job.job_id)

	def test_the_log_is_valid_utf8_and_never_cut_inside_a_character(self) -> None:
		path = self.result("tum")
		manager = self.manager(log_limit=256)
		job = manager.start(self.spec(path))
		self.wait_state(manager, job.job_id, "running")
		for number in range(200):
			self.runner.processes[0].emit("场景图聚类 %d 完成" % number)
		self.runner.processes[0].finish(1)
		self.wait_state(manager, job.job_id, "failed")
		text = (self.state / "jobs" / job.job_id / "log.txt").read_bytes().decode("utf-8")
		self.assertIn("场景图聚类 199 完成", text)
		self.assertEqual(manager.log_tail("nope", 5), [])

	def test_terminal_control_sequences_are_removed_from_log_lines(self) -> None:
		path = self.result("tum")
		manager = self.manager()
		job = manager.start(self.spec(path))
		self.wait_state(manager, job.job_id, "running")
		self.runner.processes[0].emit("\x1b[32mgreen\x1b[0m text\r")
		self.runner.processes[0].finish(1)
		self.wait_state(manager, job.job_id, "failed")
		self.assertEqual(manager.log_tail(job.job_id, 3), ["green text"])


class JobCancelTest(JobTestCase):
	def test_a_queued_job_is_cancelled_without_ever_starting(self) -> None:
		a, b = self.result("a"), self.result("b")
		manager = self.manager()
		first = manager.start(self.spec(a, run_id="a"))
		second = manager.start(self.spec(b, run_id="b"))
		self.wait_state(manager, first.job_id, "running")
		cancelled = manager.cancel(second.job_id)
		self.assertEqual(cancelled.state, "cancelled")
		self.assertEqual(len(self.runner.calls), 1)
		self.finish_with_files(self.runner.processes[0], a, "dsg_updated.json", "clustered_dsg.json")
		self.wait_state(manager, first.job_id, "succeeded")
		self.assertEqual(len(self.runner.calls), 1, "the cancelled job is not started afterwards")

	def test_cancelling_stops_the_container_and_leaves_the_client_alone(self) -> None:
		path = self.result("tum")
		manager = self.manager()
		job = manager.start(self.spec(path))
		self.wait_state(manager, job.job_id, "running")
		manager.cancel(job.job_id)
		done = self.wait_state(manager, job.job_id, "cancelled")
		self.assertEqual(set(self.stopped), {job.job_id}, "docker stop names exactly this job's container")
		self.assertEqual(self.runner.processes[0].signals, [], "the client ends with its container: it is not signalled")
		self.assertEqual(done.exit_code, 143)
		self.assertEqual([record.state for record in self.finished], ["cancelled"])
		self.assertEqual([event.payload["job"]["state"] for event in self.events("job-state")][-1], "cancelled")

	def test_when_the_container_cannot_be_stopped_the_client_is_signalled_and_finally_killed(self) -> None:
		path = self.result("tum")
		self.stop_effect = False
		self.runner.options = {"ignore_term": True}
		manager = self.manager()
		job = manager.start(self.spec(path))
		self.wait_state(manager, job.job_id, "running")
		manager.cancel(job.job_id)
		self.wait_state(manager, job.job_id, "cancelled")
		self.assertEqual(set(self.stopped), {job.job_id})
		self.assertGreaterEqual(len(self.stopped), 2, "a container that may not exist yet is asked again")
		self.assertEqual(self.runner.processes[0].signals, ["TERM", "KILL"])
		other = self.result("other")
		self.runner.options = {}
		second = manager.start(self.spec(other, run_id="b"))
		self.wait_state(manager, second.job_id, "running")
		manager.cancel(second.job_id)
		self.wait_state(manager, second.job_id, "cancelled")
		self.assertEqual(self.runner.processes[1].signals, ["TERM"], "no kill once the client gave in")

	def test_cancelling_an_unknown_or_finished_job_is_an_error(self) -> None:
		path = self.result("tum")
		manager = self.manager()
		with self.assertRaises(JobNotFound):
			manager.cancel("0123456789abcdef")
		with self.assertRaises(JobNotFound):
			manager.cancel("../../etc")
		job = manager.start(self.spec(path))
		self.wait_state(manager, job.job_id, "running")
		self.runner.processes[0].finish(1)
		self.wait_state(manager, job.job_id, "failed")
		with self.assertRaises(JobStateError):
			manager.cancel(job.job_id)
		self.assertEqual(self.stopped, [])

	def test_a_cancel_that_comes_too_late_does_not_hide_a_finished_job(self) -> None:
		path = self.result("tum")
		self.stop_effect = False
		self.runner.options = {"ignore_term": True}
		manager = self.manager(grace=5.0)
		job = manager.start(self.spec(path))
		self.wait_state(manager, job.job_id, "running")
		manager.cancel(job.job_id)
		self.finish_with_files(self.runner.processes[0], path, "dsg_updated.json", "clustered_dsg.json")
		done = self.wait_state(manager, job.job_id, "succeeded")
		self.assertTrue(done.cancel_requested)
		other = self.result("other")
		second = manager.start(self.spec(other, run_id="b"))
		self.wait_state(manager, second.job_id, "running")
		manager.cancel(second.job_id)
		self.runner.processes[1].finish(0)   # exit 0 but nothing written: the cancel stands
		self.wait_state(manager, second.job_id, "cancelled")

	def test_cancelling_twice_is_harmless(self) -> None:
		path = self.result("tum")
		self.stop_effect = False
		self.runner.options = {"ignore_term": True}
		manager = self.manager(grace=5.0)
		job = manager.start(self.spec(path))
		self.wait_state(manager, job.job_id, "running")
		first = manager.cancel(job.job_id)
		second = manager.cancel(job.job_id)
		self.assertTrue(first.cancel_requested and second.cancel_requested)
		self.wait_for(lambda: self.stopped, message="the container to be asked to stop")
		self.assertEqual(self.runner.processes[0].signals, [], "the client is left alone while the container is being stopped")
		self.runner.processes[0].finish(143)
		self.wait_state(manager, job.job_id, "cancelled")

class JobContainerTest(JobTestCase):
	"""The child process is only the docker client; the job is the container, which outlives a killed client."""

	def test_a_job_is_not_over_until_its_container_is_gone(self) -> None:
		path = self.result("tum")
		release = threading.Event()
		manager = self.manager(grace=5.0, stop_wait=5.0)   # patience: the container is stopped, just not at once

		def slow_stop(job_id: str) -> None:
			self.stopped.append(job_id)
			if release.is_set():
				self.alive.discard(job_id)

		manager.stop_container = slow_stop
		job = manager.start(self.spec(path))
		self.wait_state(manager, job.job_id, "running")
		self.alive.add(job.job_id)   # the container keeps running
		self.runner.processes[0].finish(130)   # ... after its client was killed from outside
		self.wait_for(lambda: self.stopped, message="the container to be asked to stop")
		time.sleep(0.2)
		self.assertEqual(manager.get(job.job_id).state, "running", "a container that still runs keeps the job running")
		release.set()
		failed = self.wait_state(manager, job.job_id, "failed")
		self.assertEqual(failed.exit_code, 130)

	def test_a_container_that_never_stops_does_not_hold_the_job_forever(self) -> None:
		path = self.result("tum")
		manager = self.manager(stop_container=lambda job_id: self.stopped.append(job_id))
		job = manager.start(self.spec(path))
		self.wait_state(manager, job.job_id, "running")
		self.alive.add(job.job_id)
		self.runner.processes[0].finish(1)
		self.wait_state(manager, job.job_id, "failed")   # given up after grace + stop_wait
		self.assertTrue(self.stopped)

	def test_files_a_killed_container_left_behind_are_cleaned_up_and_an_interrupted_publish_is_rolled_back(self) -> None:
		path = self.result("tum", "dsg_updated.json", "clustered_dsg.json")
		(path / "clustered_dsg.json").write_text("NEW-HALF-PUBLISHED", encoding="utf-8")
		(path / ".replay-job-12.clustered_dsg.json.bak").write_text("PREVIOUS", encoding="utf-8")
		(path / ".replay-job-12-dsg_updated.json").write_text("temporary", encoding="utf-8")
		(path / ".cluster_places_input.json").write_text("temporary", encoding="utf-8")
		(path / ".keep-me").write_text("not ours", encoding="utf-8")
		manager = self.manager()
		job = manager.start(self.spec(path, replace=True))
		self.wait_state(manager, job.job_id, "running")
		manager.cancel(job.job_id)
		self.wait_state(manager, job.job_id, "cancelled")
		self.assertEqual(sorted(entry.name for entry in path.iterdir() if entry.name.startswith(".")), [".keep-me"])
		self.assertEqual((path / "clustered_dsg.json").read_text(encoding="utf-8"), "PREVIOUS")

	def test_a_successful_job_leaves_the_directory_alone(self) -> None:
		path = self.result("tum")
		(path / ".replay-job-1-x").write_text("someone else's", encoding="utf-8")
		manager = self.manager()
		job = manager.start(self.spec(path))
		self.wait_state(manager, job.job_id, "running")
		self.finish_with_files(self.runner.processes[0], path, "dsg_updated.json", "clustered_dsg.json")
		self.wait_state(manager, job.job_id, "succeeded")
		self.assertTrue((path / ".replay-job-1-x").exists())

	def test_recover_leftovers_touches_only_the_scripts_own_names(self) -> None:
		directory = self.root / "result"
		directory.mkdir()
		(directory / "clustered_dsg.json").write_text("NEW", encoding="utf-8")
		(directory / ".replay-job-7.clustered_dsg.json.bak").write_text("OLD", encoding="utf-8")
		(directory / ".replay-job-7-clustered_dsg.json").write_text("tmp", encoding="utf-8")
		(directory / ".cluster_places_output.json").write_text("tmp", encoding="utf-8")
		(directory / ".replayjob").write_text("other", encoding="utf-8")
		(directory / "replay-job-1").write_text("visible file", encoding="utf-8")
		done = recover_leftovers(directory)
		self.assertEqual(sorted(done), ["removed .cluster_places_output.json", "removed .replay-job-7-clustered_dsg.json", "restored clustered_dsg.json"])
		self.assertEqual(sorted(entry.name for entry in directory.iterdir()), [".replayjob", "clustered_dsg.json", "replay-job-1"])
		self.assertEqual((directory / "clustered_dsg.json").read_text(encoding="utf-8"), "OLD")
		self.assertEqual(recover_leftovers(directory), [])
		self.assertEqual(recover_leftovers(self.root / "missing"), [])

	def test_the_container_check_asks_docker_for_exactly_this_container(self) -> None:
		calls = []

		def run(output: str, code: int = 0):
			def fake(argv: List[str], **kwargs: Any) -> Any:
				calls.append(argv)
				return subprocess.CompletedProcess(argv, code, stdout=output, stderr="")
			return fake

		job_id = "0123456789abcdef"
		self.assertTrue(docker_container_alive(job_id, run("a1b2c3\n")))
		self.assertEqual(calls[0], ["docker", "ps", "-q", "--filter", "name=^daaam-replay-job-0123456789abcdef$"])
		self.assertFalse(docker_container_alive(job_id, run("")))
		self.assertFalse(docker_container_alive(job_id, run("a1b2c3\n", code=1)), "docker that cannot answer does not hold a job")
		self.assertFalse(docker_container_alive("../../etc", run("a1b2c3\n")))
		self.assertEqual(len(calls), 3, "an invalid id never reaches docker")

		def missing(argv: List[str], **kwargs: Any) -> Any:
			raise FileNotFoundError("docker")

		self.assertFalse(docker_container_alive(job_id, missing))


class JobRecoveryTest(JobTestCase):
	def write_record(self, job_id: str, state: str, path: Path, **extra: Any) -> None:
		directory = self.state / "jobs" / job_id
		directory.mkdir(parents=True)
		record = {
			"schema": 1, "job_id": job_id, "run_id": "old", "result_path": str(path), "dataset_kind": "tum", "stage": "postprocess",
			"replace": False, "state": state, "created": 1000.0, "started": 1001.0 if state != "queued" else None, "finished": None,
			"exit_code": None, "message": None, "cancel_requested": False,
		}
		record.update(extra)
		(directory / "job.json").write_text(json.dumps(record), encoding="utf-8")
		(directory / "log.txt").write_text("=== earlier output\n", encoding="utf-8")

	def test_unfinished_jobs_of_an_earlier_service_become_interrupted(self) -> None:
		path = self.result("tum")
		(path / ".replay-job-46-dsg_updated.json").write_text("left by a killed container", encoding="utf-8")
		self.write_record("aaaaaaaaaaaaaaaa", "running", path)
		self.write_record("bbbbbbbbbbbbbbbb", "queued", path)
		self.write_record("cccccccccccccccc", "succeeded", path, exit_code=0, finished=1100.0)
		manager = self.manager()
		self.assertEqual(manager.get("aaaaaaaaaaaaaaaa").state, "interrupted")
		self.assertEqual(manager.get("bbbbbbbbbbbbbbbb").state, "interrupted")
		self.assertEqual(manager.get("cccccccccccccccc").state, "succeeded")
		self.assertIn("restarted", manager.get("aaaaaaaaaaaaaaaa").message)
		self.assertIsNotNone(manager.get("aaaaaaaaaaaaaaaa").finished)
		saved = json.loads((self.state / "jobs" / "aaaaaaaaaaaaaaaa" / "job.json").read_text(encoding="utf-8"))
		self.assertEqual(saved["state"], "interrupted", "the correction is persisted")
		self.assertEqual(self.stopped, ["aaaaaaaaaaaaaaaa"], "a container that may still run is stopped by name")
		self.assertFalse(list(path.glob(".replay-job-*")), "what it left behind is cleaned up")
		self.assertEqual(manager.log_tail("aaaaaaaaaaaaaaaa", 5), ["=== earlier output"])
		self.assertEqual(self.runner.calls, [])
		again = manager.start(self.spec(path))
		self.assertIsNotNone(again, "an interrupted job does not block the result")

	def test_damaged_records_are_skipped_not_fatal(self) -> None:
		path = self.result("tum")
		self.write_record("aaaaaaaaaaaaaaaa", "succeeded", path)
		broken = self.state / "jobs" / "bbbbbbbbbbbbbbbb"
		broken.mkdir(parents=True)
		(broken / "job.json").write_text("{", encoding="utf-8")
		odd = self.state / "jobs" / "not-a-job"
		odd.mkdir()
		(odd / "job.json").write_text(json.dumps({"job_id": "x"}), encoding="utf-8")
		manager = self.manager()
		self.assertEqual(manager.get("aaaaaaaaaaaaaaaa").state, "succeeded")
		with self.assertRaises(JobNotFound):
			manager.get("bbbbbbbbbbbbbbbb")

	def test_a_job_history_follows_the_result_not_the_run_id(self) -> None:
		path = self.result("tum")
		other = self.result("other")
		self.write_record("aaaaaaaaaaaaaaaa", "succeeded", path, created=1000.0)
		self.write_record("bbbbbbbbbbbbbbbb", "failed", path, created=2000.0)
		self.write_record("cccccccccccccccc", "succeeded", other, created=3000.0)
		manager = self.manager(path_of_run=lambda run_id: {"new-run": path}.get(run_id))
		self.assertEqual([job.job_id for job in manager.list_for_run("new-run")], ["bbbbbbbbbbbbbbbb", "aaaaaaaaaaaaaaaa"], "newest first")
		self.assertEqual(manager.list_for_run("unknown-run"), [])
		self.assertEqual([job.job_id for job in manager.list_for_result(other)], ["cccccccccccccccc"])

	def test_old_job_directories_are_pruned_but_unfinished_ones_are_kept_for_the_correction(self) -> None:
		path = self.result("tum")
		for number in range(12):
			self.write_record("%016x" % number, "succeeded", path, created=1000.0 + number)
		manager = self.manager(retain=5)
		kept = sorted(entry.name for entry in (self.state / "jobs").iterdir())
		self.assertEqual(kept, ["%016x" % number for number in range(7, 12)])
		self.assertEqual(len(manager.list_for_result(path)), 5)


class JobShutdownTest(JobTestCase):
	def test_the_shutdown_stops_running_jobs_and_marks_everything_unfinished_interrupted(self) -> None:
		a, b = self.result("a"), self.result("b")
		manager = self.manager()
		running = manager.start(self.spec(a, run_id="a"))
		queued = manager.start(self.spec(b, run_id="b"))
		self.wait_state(manager, running.job_id, "running")
		manager.shutdown()
		self.assertEqual(manager.get(running.job_id).state, "interrupted")
		self.assertEqual(manager.get(queued.job_id).state, "interrupted")
		self.assertIn("stopped", manager.get(running.job_id).message)
		self.assertEqual(set(self.stopped), {running.job_id}, "the running job's container is stopped")
		self.assertEqual(self.runner.processes[0].signals, [])
		self.assertEqual(len(self.runner.calls), 1)
		manager.shutdown()   # a second call is harmless
		reloaded = self.manager()
		self.assertEqual(reloaded.get(running.job_id).state, "interrupted")

	def test_illegal_state_transitions_are_rejected(self) -> None:
		path = self.result("tum")
		manager = self.manager()
		job = manager.start(self.spec(path))
		self.wait_state(manager, job.job_id, "running")
		self.runner.processes[0].finish(1)
		done = self.wait_state(manager, job.job_id, "failed")
		for state in ("queued", "running", "succeeded", "cancelled"):
			with self.subTest(state=state), self.assertRaises(JobStateError):
				done.transition(state)
		fresh = JobRecord(job_id="x" * 16, run_id="a", result_path=path, dataset_kind="tum", stage=JobStage.POSTPROCESS, replace=False, created=1.0)
		with self.assertRaises(JobStateError):
			fresh.transition("succeeded")   # a job cannot succeed without having run
		with self.assertRaises(JobStateError):
			fresh.transition("bogus")
		fresh.transition("running")
		fresh.transition("succeeded")


class JobAvailabilityTest(JobTestCase):
	def test_each_stage_reports_whether_it_may_run_and_whether_it_needs_confirmation(self) -> None:
		manager = self.manager()
		raw = self.result("raw")
		actions = manager.availability(raw)
		self.assertEqual(set(actions), {"postprocess", "summarize", "all"})
		self.assertEqual(actions["postprocess"], {"available": True, "confirm": False, "existing": [], "code": None, "reason": None})
		self.assertFalse(actions["summarize"]["available"])
		self.assertEqual(actions["summarize"]["code"], "prerequisite")
		self.assertIn("Postprocess", actions["summarize"]["reason"])
		self.assertTrue(actions["all"]["available"])
		clustered = self.result("clustered", "dsg_updated.json", "clustered_dsg.json")
		actions = manager.availability(clustered)
		self.assertEqual((actions["postprocess"]["available"], actions["postprocess"]["confirm"]), (True, True))
		self.assertEqual(actions["postprocess"]["existing"], ["dsg_updated.json", "clustered_dsg.json"])
		self.assertEqual((actions["summarize"]["available"], actions["summarize"]["confirm"]), (True, False))
		self.assertEqual((actions["all"]["available"], actions["all"]["confirm"]), (True, True))
		self.assertEqual(manager.availability(self.root / "missing")["postprocess"]["code"], "prerequisite")

	def test_without_an_api_key_only_the_summary_stages_are_unavailable(self) -> None:
		manager = self.manager(api_key_check=lambda: False)
		actions = manager.availability(self.result("clustered", "clustered_dsg.json"))
		self.assertTrue(actions["postprocess"]["available"])
		for stage in ("summarize", "all"):
			self.assertEqual((actions[stage]["available"], actions[stage]["code"]), (False, "api-key"))

	def test_an_unfinished_job_blocks_every_stage_until_it_ends(self) -> None:
		path = self.result("tum")
		manager = self.manager()
		job = manager.start(self.spec(path))
		self.wait_state(manager, job.job_id, "running")
		for stage, action in manager.availability(path).items():
			with self.subTest(stage=stage):
				self.assertEqual((action["available"], action["code"]), (False, "conflict"))
		self.runner.processes[0].finish(1)
		self.wait_state(manager, job.job_id, "failed")
		self.assertTrue(manager.availability(path)["postprocess"]["available"])


class JobStateSizeTest(JobTestCase):
	def test_the_log_in_a_state_is_cut_to_a_line_and_character_budget_keeping_the_end(self) -> None:
		path = self.result("tum")
		manager = self.manager(path_of_run=lambda run_id: path if run_id == "a" else None, log_limit=10 ** 7)
		job = manager.start(self.spec(path))
		self.wait_state(manager, job.job_id, "running")
		for number in range(700):
			self.runner.processes[0].emit("line %03d" % number)
		self.runner.processes[0].finish(1)
		self.wait_state(manager, job.job_id, "failed")
		lines = manager.state_for_run("a")["logs"][job.job_id]
		self.assertEqual((len(lines), lines[0], lines[-1]), (500, "line 200", "line 699"))
		lines = manager.state_for_run("a", log_chars=100)["logs"][job.job_id]
		self.assertEqual(lines[-1], "line 699")
		self.assertLessEqual(sum(len(line) + 1 for line in lines), 100)
		self.assertGreater(len(lines), 5)


class JobSerializationTest(JobTestCase):
	def test_the_browser_view_of_a_job_has_no_host_path_and_is_json(self) -> None:
		path = self.result("tum")
		manager = self.manager()
		job = manager.start(self.spec(path, stage=JobStage.ALL))
		view = manager.get(job.job_id).to_json()
		self.assertEqual(set(view), {
			"job_id", "run_id", "stage", "dataset_kind", "replace", "state", "created", "started", "finished", "exit_code", "message", "cancel_requested",
		})
		self.assertNotIn(str(path), json.dumps(view))
		self.assertEqual(view["stage"], "all")
		self.assertEqual(json.loads(json.dumps(view)), view)
		self.wait_state(manager, job.job_id, "running")
		stored = json.loads((self.state / "jobs" / job.job_id / "job.json").read_text(encoding="utf-8"))
		self.assertEqual(stored["result_path"], str(path), "the service keeps the path on disk to find the history again")

	def test_the_state_of_a_result_lists_recent_jobs_and_the_log_of_unfinished_ones(self) -> None:
		path = self.result("tum")
		manager = self.manager(path_of_run=lambda run_id: path if run_id == "a" else None)
		job = manager.start(self.spec(path))
		self.wait_state(manager, job.job_id, "running")
		self.runner.processes[0].emit("=== sentence embeddings")
		self.wait_for(lambda: manager.log_tail(job.job_id, 5), message="the first log line")
		state = manager.state_for_run("a")
		self.assertEqual([item["job_id"] for item in state["jobs"]], [job.job_id])
		self.assertEqual(state["logs"], {job.job_id: ["=== sentence embeddings"]})
		self.runner.processes[0].finish(1)
		self.wait_state(manager, job.job_id, "failed")
		state = manager.state_for_run("a")
		self.assertEqual(state["logs"], {job.job_id: ["=== sentence embeddings"]}, "the newest job keeps its log for the failure display")
		self.assertEqual(manager.state_for_run("unknown"), {"jobs": [], "logs": {}, "actions": {}})


if __name__ == "__main__":
	unittest.main()
