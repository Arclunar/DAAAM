"""Processing jobs of the replay workspace: Postprocess / Summarize a result in the `daaam-ws` container.

The browser can only choose one of three stages (`JobStage`) for a result it has open. The service turns that into a fixed
command (`build_job_argv`): `docker compose run` of the existing `scripts/postprocess_coda.sh --atomic --stage S` with the
canonical result directory (and nothing else) bind-mounted at /selected-result. It is run without a shell, so no browser
text is ever interpreted as a command; TUM results get their place layer and clustering config as `-e` settings, CODa
results the script's defaults. API keys stay in the repository's `.env`, which only the container reads.

Rules the manager enforces:

* at most one unfinished job per result, and at most `max_concurrent` running jobs at all (the others wait, in order);
* summaries need `clustered_dsg.json` and an OPENAI_API_KEY; every stage needs the pipeline's `dsg.json`;
* running a stage again over its existing outputs needs `replace` (the page asks the person first);
* no job starts while another heavy DAAAM container (pipeline, depth estimation, queries ...) runs: they do not fit in
  memory together (the same rule as the justfile's `_coda-guard`);
* a job succeeded only if the child exited 0 *and* its expected outputs were written during the run;
* cancelling asks the child's process group to stop, then stops the job's own container by name, then kills the group;
* every job is a record under `<state>/jobs/<id>/` (job.json, and a log.txt kept to the last `log_limit` bytes); a service
  restart turns records that were still queued or running into `interrupted`.

Progress is published as `job-state` and batched `job-log` events on the result's event channel. Standard library only,
Python 3.8.
"""

import collections
import dataclasses
import enum
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Tuple

from scripts.replay_web.manifest import write_json_atomic

JOB_ID = re.compile(r"^[0-9a-f]{16}$")
CONTAINER_PREFIX = "daaam-replay-"   # containers of the workspace itself: never counted as "another pipeline"
JOB_CONTAINER_PREFIX = CONTAINER_PREFIX + "job-"
MOUNT_POINT = "/selected-result"
COMPOSE_SERVICE = "daaam-ws"
SCRIPT = "scripts/postprocess_coda.sh"
TUM_SETTINGS = (("CLUSTER_PLACES_CONFIG", "config/cluster_places_config_tum.yaml"), ("PLACES_LAYER", "PLACES"))
CLIENT_ENV = {"COMPOSE_ANSI": "never", "COMPOSE_PROGRESS": "plain"}
HEAVY_COMMANDS = re.compile(
	r"run_coda_example|run_coda_chunks|postprocess_coda|run_foundation_stereo|run_replay|run_pipeline|run_tum_example"
	r"|run_live_preview|demo_query|daaam_node"
)
SCHEMA = 1
MAX_LINE = 2000
STATES = ("queued", "running", "succeeded", "failed", "cancelled", "interrupted")
TERMINAL = frozenset(("succeeded", "failed", "cancelled", "interrupted"))
TRANSITIONS = {
	"queued": frozenset(("running", "failed", "cancelled", "interrupted")),
	"running": frozenset(("succeeded", "failed", "cancelled", "interrupted")),
}
EXIT_NOTES = {
	130: "interrupted",
	137: "killed; probably out of memory",
	139: "crashed (segmentation fault)",
	143: "terminated",
}
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class JobStage(enum.Enum):
	POSTPROCESS = "postprocess"
	SUMMARIZE = "summarize"
	ALL = "all"

	@property
	def outputs(self) -> Tuple[str, ...]:
		"""The files the stage publishes, which also decide whether it ran: a run is only a success if they were written."""
		postprocess = ("dsg_updated.json", "clustered_dsg.json")
		summarize = ("clustered_dsg_with_summaries.json", "region_summaries.yaml")
		return {"postprocess": postprocess, "summarize": summarize, "all": postprocess + summarize}[self.value]

	@property
	def label(self) -> str:
		return {"postprocess": "Postprocess", "summarize": "Summarize", "all": "Run all"}[self.value]


class JobError(Exception):
	status = 400
	code = "job"

	def __init__(self, message: str, code: Optional[str] = None):
		super().__init__(message)
		if code is not None:
			self.code = code


class JobRefused(JobError):
	"""The job cannot be started now; `code` says why: confirm, prerequisite, api-key, busy, path, shutdown."""

	STATUSES = {"confirm": 409, "busy": 409, "prerequisite": 422, "api-key": 422, "path": 400, "shutdown": 503}

	@property
	def status(self) -> int:   # type: ignore[override]
		return self.STATUSES.get(self.code, 409)


class JobConflict(JobError):
	status = 409
	code = "conflict"


class JobNotFound(JobError):
	status = 404
	code = "job"


class JobStateError(JobError):
	status = 409
	code = "state"


@dataclass(frozen=True)
class JobSpec:
	run_id: str
	result_path: Path
	dataset_kind: str
	stage: JobStage
	replace: bool = False


@dataclass
class JobRecord:
	job_id: str
	run_id: str
	result_path: Path
	dataset_kind: str
	stage: JobStage
	replace: bool
	created: float
	state: str = "queued"
	started: Optional[float] = None
	finished: Optional[float] = None
	exit_code: Optional[int] = None
	message: Optional[str] = None
	cancel_requested: bool = False

	def transition(self, state: str, *, now: Optional[float] = None, exit_code: Optional[int] = None, message: Optional[str] = None) -> "JobRecord":
		if state not in STATES:
			raise JobStateError(f"unknown job state {state!r}")
		if state not in TRANSITIONS.get(self.state, frozenset()):
			raise JobStateError(f"a {self.state} job cannot become {state}")
		now = time.time() if now is None else now
		self.state = state
		if state == "running":
			self.started = now
		if state in TERMINAL:
			self.finished = now
			self.exit_code = exit_code
			self.message = message
		return self

	def to_json(self) -> Dict[str, Any]:
		"""What the browser sees: no host paths."""
		return {
			"job_id": self.job_id, "run_id": self.run_id, "stage": self.stage.value, "dataset_kind": self.dataset_kind,
			"replace": self.replace, "state": self.state, "created": self.created, "started": self.started,
			"finished": self.finished, "exit_code": self.exit_code, "message": self.message,
			"cancel_requested": self.cancel_requested,
		}

	def to_storage(self) -> Dict[str, Any]:
		stored = self.to_json()
		stored["schema"] = SCHEMA
		stored["result_path"] = str(self.result_path)
		return stored

	@staticmethod
	def from_storage(value: Mapping[str, Any]) -> "JobRecord":
		if not isinstance(value, dict) or value.get("schema") != SCHEMA:
			raise ValueError("not a job record")
		job_id = value.get("job_id")
		if not isinstance(job_id, str) or not JOB_ID.match(job_id):
			raise ValueError("bad job id")
		state = value.get("state")
		if state not in STATES:
			raise ValueError("bad job state")
		path = value.get("result_path")
		if not isinstance(path, str) or not os.path.isabs(path):
			raise ValueError("bad result path")
		record = JobRecord(
			job_id=job_id, run_id=str(value.get("run_id") or ""), result_path=Path(path), dataset_kind=str(value.get("dataset_kind") or ""),
			stage=JobStage(value.get("stage")), replace=bool(value.get("replace")), created=float(value["created"]), state=state,
			started=_number(value.get("started")), finished=_number(value.get("finished")),
			exit_code=value.get("exit_code") if isinstance(value.get("exit_code"), int) else None,
			message=value.get("message") if isinstance(value.get("message"), str) else None,
			cancel_requested=bool(value.get("cancel_requested")),
		)
		return record


def _number(value: Any) -> Optional[float]:
	return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# ---------------------------------------------------------------------------------------------- the command
def build_job_argv(spec: JobSpec, job_id: str, repo_root: Path) -> Tuple[List[str], Dict[str, str]]:
	"""The only command a job runs, as an argument list for a shell-less exec, and the settings for the docker client.

	The result path is one argument (`-v PATH:/selected-result`); the stage is an enum value. Nothing else varies.
	"""
	argv = [
		"docker", "compose", "run", "--rm", "-T",
		"--name", JOB_CONTAINER_PREFIX + job_id,
		"-v", "%s:%s" % (spec.result_path, MOUNT_POINT),
	]
	if spec.dataset_kind == "tum":
		for name, value in TUM_SETTINGS:
			argv += ["-e", "%s=%s" % (name, value)]
	argv += [COMPOSE_SERVICE, "bash", SCRIPT, "--atomic", "--stage", spec.stage.value, MOUNT_POINT]
	return argv, dict(CLIENT_ENV)


class GroupProcess:
	"""A child started in a session of its own, so that it and whatever it started can be signalled together."""

	def __init__(self, popen: "subprocess.Popen[str]"):
		self._popen = popen
		self.pid = popen.pid
		self.stdout = popen.stdout

	def wait(self, timeout: Optional[float] = None) -> int:
		return self._popen.wait(timeout)

	def poll(self) -> Optional[int]:
		return self._popen.poll()

	def _signal(self, number: int) -> None:
		if self._popen.poll() is not None:
			return   # already reaped: the process group id may belong to someone else by now
		try:
			os.killpg(self.pid, number)
		except (ProcessLookupError, PermissionError):
			pass

	def terminate_group(self) -> None:
		self._signal(signal.SIGTERM)

	def kill_group(self) -> None:
		self._signal(signal.SIGKILL)


def popen_runner(argv: List[str], env: Dict[str, str], cwd: Path) -> GroupProcess:
	merged = dict(os.environ)
	merged.update(env)
	popen = subprocess.Popen(
		argv, cwd=str(cwd), env=merged, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
		text=True, encoding="utf-8", errors="replace", bufsize=1, start_new_session=True,
	)
	return GroupProcess(popen)


def docker_stop(job_id: str) -> None:
	"""`docker stop` of exactly this job's container (a fixed command; the id is generated here and checked)."""
	if not JOB_ID.match(job_id):
		return
	try:
		subprocess.run(["docker", "stop", "-t", "5", JOB_CONTAINER_PREFIX + job_id], stdin=subprocess.DEVNULL,
			stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
	except (OSError, subprocess.SubprocessError):
		pass


def docker_busy_reason(run: Callable[..., Any] = subprocess.run) -> Optional[str]:
	"""Why a job must not start now (another heavy DAAAM container runs, or docker does not answer), else None."""
	try:
		result = run(["docker", "ps", "--no-trunc", "--format", "{{.Names}}\t{{.Command}}"], stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=20)
	except FileNotFoundError:
		return "docker is not installed or not on the service's PATH"
	except (OSError, subprocess.SubprocessError) as error:
		return "docker does not answer (%s)" % type(error).__name__
	if result.returncode != 0:
		detail = (result.stderr or "").strip().splitlines()
		return "docker does not answer (docker ps failed%s)" % (": " + detail[0] if detail else "")
	for line in (result.stdout or "").splitlines():
		name, _, command = line.partition("\t")
		if name.startswith(CONTAINER_PREFIX):
			continue
		found = HEAVY_COMMANDS.search(command)
		if found:
			return "another DAAAM container is running (%s); jobs cannot start while a pipeline, depth estimate, post-processing or query runs, because they do not fit in memory together" % found.group(0)
	return None


def has_openai_key(repo_root: Path, environ: Optional[Mapping[str, str]] = None) -> bool:
	"""Whether OPENAI_API_KEY is set in the environment or in the repository's .env (only the presence is looked at)."""
	if (environ if environ is not None else os.environ).get("OPENAI_API_KEY"):
		return True
	try:
		for line in (Path(repo_root) / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
			if line.startswith("OPENAI_API_KEY=") and len(line.rstrip("\r\n")) > len("OPENAI_API_KEY="):
				return True
	except OSError:
		pass
	return False


# ---------------------------------------------------------------------------------------------- logs
def clean_line(raw: str) -> str:
	"""A log line without terminal control sequences or carriage returns (progress bars keep their last state), cut short."""
	text = _ANSI.sub("", raw.rstrip("\n"))
	text = text.split("\r")[-1] if "\r" in text.rstrip("\r") else text.rstrip("\r")
	return text if len(text) <= MAX_LINE else text[:MAX_LINE] + "…"


class _LogFile:
	"""The log of one job: appended line by line, cut to its last `limit` bytes (at a line start) whenever it reaches twice that."""

	def __init__(self, path: Path, limit: int):
		self.path = path
		self.limit = limit
		self.lines = 0
		try:
			self.size = path.stat().st_size
		except OSError:
			self.size = 0

	def append(self, lines: List[str]) -> None:
		data = "".join(line + "\n" for line in lines).encode("utf-8")
		self.path.parent.mkdir(parents=True, exist_ok=True)
		with self.path.open("ab") as stream:
			stream.write(data)
		self.size += len(data)
		self.lines += len(lines)
		if self.size > 2 * self.limit:
			self._trim()

	def _trim(self) -> None:
		tail = self.path.read_bytes()[-self.limit:]
		cut = tail.find(b"\n")
		tail = tail[cut + 1:] if cut >= 0 else b""
		temporary = self.path.with_name(self.path.name + ".tmp")
		temporary.write_bytes(tail)
		os.replace(str(temporary), str(self.path))
		self.size = len(tail)

	def tail(self, count: int) -> List[str]:
		try:
			text = self.path.read_bytes().decode("utf-8", errors="replace")
		except OSError:
			return []
		return text.splitlines()[-count:] if count > 0 else []


class _LineBuffer:
	"""Collects a job's output lines and hands them on in batches: when 100 are waiting, or 0.15 s after the first one."""

	def __init__(self, sink: Callable[[List[str]], None], interval: float = 0.15, max_lines: int = 100):
		self._sink = sink
		self._interval = interval
		self._max_lines = max_lines
		self._lock = threading.Lock()
		self._flush_lock = threading.Lock()   # batches reach the sink one at a time, in order
		self._lines: List[str] = []
		self._timer: Optional[threading.Timer] = None

	def add(self, line: str) -> None:
		with self._lock:
			self._lines.append(line)
			due = len(self._lines) >= self._max_lines
			if not due and self._timer is None:
				self._timer = threading.Timer(self._interval, self.flush)
				self._timer.daemon = True
				self._timer.start()
		if due:
			self.flush()

	def flush(self) -> None:
		with self._flush_lock:
			with self._lock:
				lines, self._lines = self._lines, []
				timer, self._timer = self._timer, None
			if timer is not None:
				timer.cancel()
			if lines:
				self._sink(lines)


# ---------------------------------------------------------------------------------------------- the manager
@dataclass
class _Active:
	"""A job that holds a running slot: launching, running, or being stopped."""

	process: Optional[Any] = None
	interrupt: bool = False   # the service is stopping
	stop_started: bool = False   # the process group has been asked to stop
	done: threading.Event = dataclasses.field(default_factory=threading.Event)


def _describe_exit(code: int) -> str:
	note = EXIT_NOTES.get(code)
	return "exit status %d%s" % (code, " (%s)" % note if note else "")


class JobManager:
	def __init__(self, state_root: Path, broker: Optional[Any] = None, runner: Optional[Callable[..., Any]] = None,
			repo_root: Optional[Path] = None, max_concurrent: int = 1, api_key_check: Optional[Callable[[], bool]] = None,
			busy_check: Optional[Callable[[], Optional[str]]] = None, stop_container: Optional[Callable[[str], None]] = None,
			on_finished: Optional[Callable[[JobRecord], None]] = None, grace: float = 10.0, stop_wait: float = 5.0,
			log_limit: int = 256 * 1024, path_of_run: Optional[Callable[[str], Optional[Path]]] = None,
			run_of_path: Optional[Callable[[Path], Optional[str]]] = None, retain: int = 50):
		self.jobs_dir = Path(state_root) / "jobs"
		self.broker = broker
		self.runner = runner or popen_runner
		self.repo_root = Path(repo_root) if repo_root is not None else Path.cwd()
		self.max_concurrent = max(1, int(max_concurrent))
		self.api_key_check = api_key_check or (lambda: has_openai_key(self.repo_root))
		self.busy_check = busy_check or docker_busy_reason
		self.stop_container = stop_container or docker_stop
		self.on_finished = on_finished
		self.grace = grace
		self.stop_wait = stop_wait
		self.log_limit = log_limit
		self.path_of_run = path_of_run
		self.run_of_path = run_of_path
		self.retain = retain
		self._lock = threading.RLock()
		self._jobs: Dict[str, JobRecord] = {}
		self._queue: Deque[str] = collections.deque()
		self._active: Dict[str, _Active] = {}
		self._logs: Dict[str, _LogFile] = {}
		self._buffers: Dict[str, _LineBuffer] = {}
		self._threads: List[threading.Thread] = []
		self._shutdown = False
		self._load()

	# ------------------------------------------------------------------ persistence and recovery
	def _job_dir(self, job_id: str) -> Path:
		return self.jobs_dir / job_id

	def _persist(self, job: JobRecord) -> None:
		try:
			self._job_dir(job.job_id).mkdir(parents=True, exist_ok=True)
			write_json_atomic(self._job_dir(job.job_id) / "job.json", job.to_storage())
		except OSError as error:
			sys.stderr.write(f"[replay-web] cannot save job {job.job_id}: {error}\n")

	def _log(self, job_id: str) -> _LogFile:
		with self._lock:
			log = self._logs.get(job_id)
			if log is None:
				log = self._logs[job_id] = _LogFile(self._job_dir(job_id) / "log.txt", self.log_limit)
			return log

	def _load(self) -> None:
		"""Read the records of earlier service runs. A job that was still queued or running then cannot be running now."""
		if not self.jobs_dir.is_dir():
			return
		for entry in sorted(self.jobs_dir.iterdir()):
			if not JOB_ID.match(entry.name):
				continue
			try:
				record = JobRecord.from_storage(json.loads((entry / "job.json").read_text(encoding="utf-8")))
				if record.job_id != entry.name:
					raise ValueError("record does not match its directory")
			except (OSError, ValueError, KeyError, TypeError) as error:
				sys.stderr.write(f"[replay-web] ignoring the damaged job record {entry.name}: {error}\n")
				continue
			was_running = record.state == "running"
			if record.state not in TERMINAL:
				record.transition("interrupted", message="the replay service restarted while this job had not finished")
				self._persist(record)
				if was_running:
					try:
						self.stop_container(record.job_id)   # its container may still be running
					except Exception:
						traceback.print_exc()
			self._jobs[record.job_id] = record
		self._prune()

	def _prune(self) -> None:
		by_result: Dict[Path, List[JobRecord]] = {}
		for record in self._jobs.values():
			by_result.setdefault(record.result_path, []).append(record)
		for records in by_result.values():
			records.sort(key=lambda record: record.created, reverse=True)
			for record in records[self.retain:]:
				if record.state in TERMINAL:
					del self._jobs[record.job_id]
					shutil.rmtree(str(self._job_dir(record.job_id)), ignore_errors=True)

	# ------------------------------------------------------------------ queries
	def get(self, job_id: str) -> JobRecord:
		with self._lock:
			job = self._jobs.get(job_id) if isinstance(job_id, str) and JOB_ID.match(job_id) else None
			if job is None:
				raise JobNotFound("no such job")
			return dataclasses.replace(job)

	def list_for_result(self, path: Path) -> List[JobRecord]:
		"""The jobs of a result, newest first."""
		with self._lock:
			found = [dataclasses.replace(job) for job in self._jobs.values() if job.result_path == Path(path)]
		return sorted(found, key=lambda job: job.created, reverse=True)

	def list_for_run(self, run_id: str) -> List[JobRecord]:
		"""The jobs of the result a run ID names now. After a restart the same result has a new ID and the same history."""
		path = self.path_of_run(run_id) if self.path_of_run is not None else None
		if path is not None:
			return self.list_for_result(path)
		with self._lock:
			found = [dataclasses.replace(job) for job in self._jobs.values() if job.run_id == run_id]
		return sorted(found, key=lambda job: job.created, reverse=True)

	def log_tail(self, job_id: str, lines: int = 100) -> List[str]:
		if not isinstance(job_id, str) or not JOB_ID.match(job_id):
			return []
		with self._lock:
			if job_id not in self._jobs:
				return []
		return self._log(job_id).tail(lines)

	def state_for_run(self, run_id: str, recent: int = 5, log_lines: int = 100) -> Dict[str, Any]:
		"""What a page needs to show a result's jobs: the newest few, the output of those still running and of the newest,
		and what each stage may do now (`availability`)."""
		jobs = self.list_for_run(run_id)[:recent]
		logs = {}
		for index, job in enumerate(jobs):
			if index == 0 or job.state not in TERMINAL:
				tail = self.log_tail(job.job_id, log_lines)
				if tail:
					logs[job.job_id] = tail
		path = self.path_of_run(run_id) if self.path_of_run is not None else (jobs[0].result_path if jobs else None)
		return {"jobs": [job.to_json() for job in jobs], "logs": logs, "actions": self.availability(path) if path is not None else {}}

	# ------------------------------------------------------------------ starting
	def start(self, spec: JobSpec) -> JobRecord:
		if not isinstance(spec.stage, JobStage):
			raise JobRefused("the stage must be postprocess, summarize or all", "stage")
		self._check_open()
		path = self._checked_path(spec.result_path)
		self._check_free(path)
		self._check_prerequisites(spec, path)
		reason = self.busy_check()   # asks docker: not under the lock
		if reason:
			raise JobRefused(reason, "busy")
		with self._lock:
			self._check_open()
			self._check_free(path)
			job = JobRecord(job_id=secrets.token_hex(8), run_id=spec.run_id, result_path=path, dataset_kind=spec.dataset_kind,
				stage=spec.stage, replace=spec.replace, created=time.time())
			self._jobs[job.job_id] = job
			self._queue.append(job.job_id)
			snapshot = dataclasses.replace(job)
		self._persist(snapshot)
		self._publish_state(snapshot)
		self._schedule()
		return self.get(job.job_id)

	def _check_open(self) -> None:
		with self._lock:
			if self._shutdown:
				raise JobRefused("the replay service is shutting down", "shutdown")

	@staticmethod
	def _checked_path(path: Path) -> Path:
		text = str(path)
		if not text or not os.path.isabs(text) or any(char in text for char in (":", "\n", "\r", "\x00")):
			raise JobRefused("this result's location cannot be mounted into the job container safely", "path")
		return Path(text)

	def _check_free(self, path: Path) -> None:
		with self._lock:
			for job in self._jobs.values():
				if job.result_path == path and job.state in ("queued", "running"):
					raise JobConflict("job %s of this result is still %s: wait for it or cancel it" % (job.job_id, job.state))

	@staticmethod
	def _present(path: Path, name: str) -> bool:
		try:
			return (path / name).stat().st_size > 0
		except OSError:
			return False

	def _refusal(self, stage: JobStage, path: Path) -> Optional[Tuple[str, str]]:
		"""Why a stage cannot run on this result at all, as (code, message); None when it can."""
		if not (path / "dsg.json").is_file():
			return "prerequisite", "this result has no dsg.json: only a finished mapping result can be processed"
		if stage is JobStage.SUMMARIZE and not self._present(path, "clustered_dsg.json"):
			return "prerequisite", "Summarize needs clustered_dsg.json: run Postprocess first"
		if stage is not JobStage.POSTPROCESS and not self.api_key_check():
			return "api-key", "the region summaries are written by OpenAI: put OPENAI_API_KEY=... into the .env file in the repository directory"
		return None

	def _check_prerequisites(self, spec: JobSpec, path: Path) -> None:
		refusal = self._refusal(spec.stage, path)
		if refusal is not None:
			raise JobRefused(refusal[1], refusal[0])
		existing = [name for name in spec.stage.outputs if (path / name).exists()]
		if existing and not spec.replace:
			raise JobRefused("%s already produced %s; confirm to run it again and replace them" % (spec.stage.label, ", ".join(existing)), "confirm")

	def availability(self, path: Path) -> Dict[str, Dict[str, Any]]:
		"""What each stage can do for a result right now, from the files and jobs on record: the page's buttons follow this.

		available: it may be started; confirm: it may, but only after the person agreed to replace `existing` outputs;
		code/reason: why it may not (conflict, prerequisite, api-key).
		"""
		path = Path(path)
		with self._lock:
			unfinished = next((job for job in self._jobs.values() if job.result_path == path and job.state in ("queued", "running")), None)
		actions: Dict[str, Dict[str, Any]] = {}
		for stage in JobStage:
			existing = [name for name in stage.outputs if (path / name).exists()]
			if unfinished is not None:
				refusal: Optional[Tuple[str, str]] = ("conflict", "a job of this result is still %s" % unfinished.state)
			else:
				refusal = self._refusal(stage, path)
			actions[stage.value] = {
				"available": refusal is None, "confirm": refusal is None and bool(existing), "existing": existing,
				"code": refusal[0] if refusal else None, "reason": refusal[1] if refusal else None,
			}
		return actions

	# ------------------------------------------------------------------ scheduling and running
	def _spawn(self, target: Callable[..., None], args: Tuple[Any, ...], name: str) -> None:
		"""Run `target` on a thread the shutdown waits for."""
		thread = threading.Thread(target=target, args=args, name=name, daemon=True)
		with self._lock:
			self._threads = [other for other in self._threads if other.is_alive()]
			self._threads.append(thread)
		thread.start()

	def _schedule(self) -> None:
		launch: List[str] = []
		with self._lock:
			while not self._shutdown and self._queue and len(self._active) < self.max_concurrent:
				job_id = self._queue.popleft()
				job = self._jobs.get(job_id)
				if job is None or job.state != "queued":
					continue   # cancelled while it waited
				self._active[job_id] = _Active()
				launch.append(job_id)
		for job_id in launch:
			self._launch(job_id)

	def _launch(self, job_id: str) -> None:
		with self._lock:
			job = self._jobs[job_id]
			spec = JobSpec(run_id=job.run_id, result_path=job.result_path, dataset_kind=job.dataset_kind, stage=job.stage, replace=job.replace)
		reason = self.busy_check()
		if reason:
			self._conclude_unstarted(job_id, "another job or pipeline got in the way: " + reason)
			return
		argv, env = build_job_argv(spec, job_id, self.repo_root)
		try:
			process = self.runner(argv, env, self.repo_root)
		except Exception as error:
			self._conclude_unstarted(job_id, "cannot start the job: %s" % error)
			return
		with self._lock:
			active = self._active[job_id]
			active.process = process
			job = self._jobs[job_id]
			job.transition("running")
			snapshot = dataclasses.replace(job)
			stop_now = active.interrupt or job.cancel_requested
		self._persist(snapshot)
		self._publish_state(snapshot)
		self._spawn(self._pump, (job_id, process), "replay-job-" + job_id)
		if stop_now:
			self._begin_stop(job_id)

	def _conclude_unstarted(self, job_id: str, message: str) -> None:
		with self._lock:
			job = self._jobs[job_id]
			job.transition("interrupted" if self._shutdown else "failed", message=message)
			snapshot = dataclasses.replace(job)
			active = self._active.pop(job_id, None)
		self._finish(snapshot, active)

	def _pump(self, job_id: str, process: Any) -> None:
		buffer = _LineBuffer(lambda lines: self._emit_log(job_id, lines))
		with self._lock:
			self._buffers[job_id] = buffer
		try:
			for raw in process.stdout:
				buffer.add(clean_line(raw))
		except Exception:
			traceback.print_exc()
		buffer.flush()
		try:
			exit_code = process.wait()
		except Exception:
			traceback.print_exc()
			exit_code = None
		self._conclude(job_id, exit_code)

	def _emit_log(self, job_id: str, lines: List[str]) -> None:
		log = self._log(job_id)
		first = log.lines
		try:
			log.append(lines)
		except OSError as error:
			sys.stderr.write(f"[replay-web] cannot write the log of job {job_id}: {error}\n")
		self._publish(job_id, "job-log", {"job_id": job_id, "first": first, "lines": lines})

	def _missing_outputs(self, job: JobRecord) -> List[str]:
		"""Expected outputs that are absent, empty, or older than the run (left over from an earlier one)."""
		since = (job.started or job.created) - 2.0
		missing = []
		for name in job.stage.outputs:
			try:
				stat = (job.result_path / name).stat()
				fresh = stat.st_size > 0 and stat.st_mtime >= since
			except OSError:
				fresh = False
			if not fresh:
				missing.append(name)
		return missing

	def _conclude(self, job_id: str, exit_code: Optional[int]) -> None:
		with self._lock:
			job = self._jobs[job_id]
			active = self._active.get(job_id)
			if active is not None and active.interrupt:
				state, message = "interrupted", "the replay service stopped while the job was running"
			elif exit_code == 0:
				missing = self._missing_outputs(job)
				if not missing:
					state, message = "succeeded", None   # also when a cancel arrived too late: the outputs are published
				elif job.cancel_requested:
					state, message = "cancelled", "cancelled"
				else:
					state, message = "failed", "the job finished with exit status 0 but did not write %s" % ", ".join(missing)
			elif job.cancel_requested:
				state, message = "cancelled", "cancelled"
			elif exit_code is None:
				state, message = "failed", "the job's exit status could not be read"
			else:
				state, message = "failed", _describe_exit(exit_code)
			job.transition(state, exit_code=exit_code, message=message)
			snapshot = dataclasses.replace(job)
			self._active.pop(job_id, None)
			self._buffers.pop(job_id, None)
		self._finish(snapshot, active)

	def _finish(self, snapshot: JobRecord, active: Optional[_Active]) -> None:
		"""A job reached its final state: save it, tell the pages and the owner, free the slot for the next job."""
		self._persist(snapshot)
		self._publish_state(snapshot)
		if active is not None:
			active.done.set()
		if self.on_finished is not None:
			try:
				self.on_finished(snapshot)
			except Exception:
				traceback.print_exc()
		self._schedule()

	# ------------------------------------------------------------------ stopping
	def cancel(self, job_id: str) -> JobRecord:
		with self._lock:
			job = self._jobs.get(job_id) if isinstance(job_id, str) and JOB_ID.match(job_id) else None
			if job is None:
				raise JobNotFound("no such job")
			if job.state in TERMINAL:
				raise JobStateError("this job is already %s" % job.state)
			if job.state == "queued" and job_id not in self._active:
				try:
					self._queue.remove(job_id)
				except ValueError:
					pass
				job.transition("cancelled", message="cancelled before it started")
				snapshot = dataclasses.replace(job)
				immediate = True
			else:
				job.cancel_requested = True
				snapshot = dataclasses.replace(job)
				immediate = False
		if immediate:
			self._finish(snapshot, None)
			return snapshot
		self._persist(snapshot)
		self._publish_state(snapshot)
		self._begin_stop(job_id)
		return snapshot

	def _begin_stop(self, job_id: str) -> None:
		"""Start stopping a running job (once); a job still being launched is stopped as soon as its process exists."""
		with self._lock:
			active = self._active.get(job_id)
			if active is None or active.process is None or active.stop_started:
				return
			active.stop_started = True
		self._spawn(self._stop_worker, (job_id, active), "replay-stop-" + job_id)

	def _stop_worker(self, job_id: str, active: _Active) -> None:
		"""Ask the process group to stop; after `grace` stop the job's container by name; after `stop_wait` more kill the group."""
		process = active.process
		process.terminate_group()
		if active.done.wait(self.grace):
			return
		try:
			self.stop_container(job_id)
		except Exception:
			traceback.print_exc()
		if active.done.wait(self.stop_wait):
			return
		process.kill_group()

	def shutdown(self, timeout: float = 30.0) -> None:
		"""Stop taking jobs, stop the running ones and mark everything unfinished interrupted. Safe to call again."""
		with self._lock:
			if self._shutdown:
				return
			self._shutdown = True
			waiting = [job_id for job_id in self._queue if job_id not in self._active]
			self._queue.clear()
			actives = list(self._active.items())
			for _, active in actives:
				active.interrupt = True
			snapshots = []
			for job_id in waiting:
				job = self._jobs.get(job_id)
				if job is not None and job.state == "queued":
					job.transition("interrupted", message="the replay service stopped before the job started")
					snapshots.append(dataclasses.replace(job))
		for snapshot in snapshots:
			self._finish(snapshot, None)
		for job_id, active in actives:
			if active.process is not None:
				self._begin_stop(job_id)
		deadline = time.monotonic() + timeout
		for job_id, active in actives:
			active.done.wait(max(0.0, deadline - time.monotonic()))
		with self._lock:
			threads = list(self._threads)
		for thread in threads:   # the workers finish their last save and notification before this returns
			if thread is not threading.current_thread():
				thread.join(max(0.0, deadline - time.monotonic()))

	# ------------------------------------------------------------------ events
	def _channel(self, path: Path, run_id: str) -> Optional[str]:
		if self.run_of_path is not None:
			current = self.run_of_path(path)
			if current:
				return current
		return run_id or None

	def _publish(self, job_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
		if self.broker is None:
			return
		with self._lock:
			job = self._jobs.get(job_id)
			channel = self._channel(job.result_path, job.run_id) if job is not None else None
		if channel:
			try:
				self.broker.publish(channel, event_type, payload)
			except Exception:
				traceback.print_exc()

	def _publish_state(self, snapshot: JobRecord) -> None:
		self._publish(snapshot.job_id, "job-state", {"job": snapshot.to_json()})
