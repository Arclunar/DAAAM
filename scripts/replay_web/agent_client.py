"""The host-side client of the replay Agent worker, and the staging of the graph snapshots it reads.

The replay service starts one worker process (`docker compose run ... daaam-ws python -m scripts.replay_web.agent_worker`) the
first time a question needs it and talks to it in the NDJSON protocol of agent_protocol.py. Requests queue: there is one
process and it answers one request at a time, whatever the number of tabs. A worker that dies before it has streamed any of an
answer is started again once for that request; one that goes silent is stopped. It is released when idle (the service frees the
GPU for a job) and when the service ends: its input is closed first, and only a worker that does not end by itself is stopped by
its fixed container name (ending the `docker compose run` client alone would leave the container running).

Nothing but validated protocol events reaches the caller, and the worker's stderr is logged with keys masked. The standard
library only (Python 3.8).
"""

import collections
import contextlib
import hashlib
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Sequence, Union

from scripts.replay_web.agent_protocol import (
	CACHE_DIR, MAX_EVENT_BYTES, MAX_LINE_BYTES, MODELS_ENV, PROTOCOL_VERSION, Request, allowed_models, parse_event, parse_request, redact,
)
from scripts.replay_web.jobs import CONTAINER_PREFIX

AGENT_CONTAINER = CONTAINER_PREFIX + "agent"   # fixed: a stale one is removed before a start, and only this one is ever stopped
_CONTAINER_NAME = re.compile(r"daaam-replay-[a-z0-9-]{1,40}")
EventCallback = Callable[[Dict[str, Any]], None]


class AgentError(Exception):
	"""The worker could not answer. `code` is a short token the service can show or map; `message` is safe to show."""
	code = "agent-error"

	def __init__(self, message: str, code: Optional[str] = None):
		super().__init__(message)
		self.message = message
		if code is not None:
			self.code = code


class AgentUnavailable(AgentError):
	"""There is no worker: it cannot be started, or the client is shutting down."""
	code = "worker-unavailable"


class AgentCrashed(AgentError):
	"""The worker died while answering (and starting it again once did not help, or part of the answer had been streamed)."""
	code = "worker-crashed"


class AgentTimeout(AgentError):
	"""The worker stopped answering; it was stopped and will be started again for the next request."""
	code = "timeout"


class AgentRequestFailed(AgentError):
	"""The worker reported an error for this request (its code and message come from the worker, validated)."""


def default_argv(models: Sequence[str], idle_seconds: Optional[float] = None) -> List[str]:
	"""The fixed worker command. The only variable parts are the model names, which agent_protocol.allowed_models vetted, and
	the worker's idle time in seconds (a number; 0 means never; by default the worker's own fifteen minutes)."""
	argv = [
		"docker", "compose", "run", "--rm", "-T", "--name", AGENT_CONTAINER,
		"-e", "HF_HOME=/data/hf_cache", "-e", "PYTHONUNBUFFERED=1", "-e", "%s=%s" % (MODELS_ENV, ",".join(models)),
	]
	if idle_seconds is not None:
		argv += ["-e", "REPLAY_AGENT_IDLE_SECONDS=%g" % float(idle_seconds)]
	return argv + ["daaam-ws", "python", "-m", "scripts.replay_web.agent_worker"]


def _docker(arguments: List[str], name: str, run: Callable[..., Any]) -> None:
	if not _CONTAINER_NAME.fullmatch(name):
		return
	try:
		run(arguments + [name], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
	except (OSError, subprocess.SubprocessError):
		pass


def docker_stop_named(name: str, run: Callable[..., Any] = subprocess.run) -> None:
	"""`docker stop` of one container of the workspace's own namespace: SIGTERM, and a kill after ten seconds."""
	_docker(["docker", "stop", "-t", "10"], name, run)


def docker_remove_named(name: str, run: Callable[..., Any] = subprocess.run) -> None:
	"""`docker rm -f` of a stale container of that name (a worker left behind by a service that was killed)."""
	_docker(["docker", "rm", "-f"], name, run)


_DIED = object()


class _WorkerDied(Exception):
	def __init__(self, after_output: bool):
		super().__init__("the worker process ended")
		self.after_output = after_output


class _Pending:
	"""One request waiting for its events."""

	def __init__(self, generation: int):
		self.generation = generation
		self.events: "queue.Queue" = queue.Queue()


class AgentClient:
	"""One shared worker process behind a FIFO queue of requests."""

	def __init__(self, argv: Optional[Sequence[str]] = None, *, models: Optional[Sequence[str]] = None, repo_root: Optional[Path] = None,
			container_name: Optional[str] = None, env: Optional[Mapping[str, str]] = None, idle_timeout: float = 300.0,
			request_timeout: float = 900.0, stop_grace: float = 10.0, spawn: Optional[Callable[..., Any]] = None,
			stop_container: Optional[Callable[[str], None]] = None, remove_container: Optional[Callable[[str], None]] = None,
			log: Optional[Callable[[str], None]] = None):
		self.models = tuple(models) if models is not None else allowed_models()
		self.repo_root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
		if argv is None:
			argv, container_name = default_argv(self.models), AGENT_CONTAINER
		self.argv = list(argv)
		self.container_name = container_name
		self.env = env
		self.idle_timeout, self.request_timeout, self.stop_grace = idle_timeout, request_timeout, stop_grace
		self._spawn = spawn or subprocess.Popen
		self._stop_container = stop_container or docker_stop_named
		self._remove_container = remove_container or docker_remove_named
		self._log = log or (lambda message: sys.stderr.write("[replay-web] agent: %s\n" % message))
		self._lock = threading.RLock()
		self._cond = threading.Condition(self._lock)
		self._waiting: Deque[object] = collections.deque()
		self._busy = False
		self._stopping = False
		self._process: Any = None
		self._generation = 0
		self._pending: Dict[str, _Pending] = {}
		self._threads: List[threading.Thread] = []
		self._writer = threading.Lock()
		self._tail: Deque[str] = collections.deque(maxlen=200)

	# ------------------------------------------------------------------ state
	@property
	def busy(self) -> bool:
		return self._busy

	@property
	def queued(self) -> int:
		return len(self._waiting)

	@property
	def alive(self) -> bool:
		process = self._process
		return process is not None and process.poll() is None

	def recent_log(self) -> List[str]:
		"""The last lines the worker wrote to stderr, keys masked."""
		return list(self._tail)

	def _note(self, message: str) -> None:
		self._log(redact(message))

	# ------------------------------------------------------------------ the turn queue
	@contextlib.contextmanager
	def _turn(self):
		"""Wait for this request's turn: one request at a time, in the order they arrived."""
		ticket = object()
		with self._cond:
			self._waiting.append(ticket)
			try:
				while not self._stopping and (self._waiting[0] is not ticket or self._busy):
					self._cond.wait()
				if self._stopping:
					raise AgentUnavailable("the replay service is shutting down")
			finally:
				self._waiting.remove(ticket)
			self._busy = True
		try:
			yield
		finally:
			with self._cond:
				self._busy = False
				self._cond.notify_all()

	# ------------------------------------------------------------------ the process
	def _ensure_process(self) -> Any:
		with self._lock:
			if self._stopping:
				raise AgentUnavailable("the replay service is shutting down")
			process = self._process
			if process is not None and process.poll() is None:
				return process
		if self.container_name:
			self._remove_container(self.container_name)
		env = dict(os.environ if self.env is None else self.env)
		env.setdefault("COMPOSE_ANSI", "never")
		env.setdefault("COMPOSE_PROGRESS", "plain")
		try:
			process = self._spawn(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(self.repo_root), env=env)
		except FileNotFoundError:
			raise AgentUnavailable("the agent worker command was not found (is docker installed and on the service's PATH?)")
		except OSError as error:
			raise AgentUnavailable("the agent worker could not be started (%s)" % (error.strerror or type(error).__name__))
		with self._lock:
			stopping = self._stopping
			if not stopping:
				self._generation += 1
				generation = self._generation
				self._process = process
				self._threads = [thread for thread in self._threads if thread.is_alive()]
				for target in (self._read_stdout, self._read_stderr):
					thread = threading.Thread(target=target, args=(process, generation), name="agent-" + target.__name__, daemon=True)
					self._threads.append(thread)
					thread.start()
		if stopping:   # stop() came while the worker was starting: it must not outlive the client
			self._terminate(process)
			raise AgentUnavailable("the replay service is shutting down")
		self._note("worker started")
		return process

	def _read_stdout(self, process: Any, generation: int) -> None:
		try:
			while True:
				line = process.stdout.readline(MAX_EVENT_BYTES + 1)
				if not line:
					break
				if len(line) > MAX_EVENT_BYTES and not line.endswith(b"\n"):
					while True:
						rest = process.stdout.readline(1 << 16)
						if not rest or rest.endswith(b"\n"):
							break
					self._note("dropped an over-long line from the worker")
					continue
				event = parse_event(line)
				if event is None:
					self._note("ignored a malformed line from the worker")
					continue
				self._dispatch(event)
		except (OSError, ValueError):
			pass
		finally:
			self._ended(process, generation)
			with contextlib.suppress(Exception):
				process.stdout.close()

	def _read_stderr(self, process: Any, generation: int) -> None:
		try:
			while True:
				raw = process.stderr.readline(1 << 16)
				if not raw:
					break
				line = redact(raw.decode("utf-8", "replace").rstrip())
				if line:
					self._tail.append(line)
					self._log(line)
		except (OSError, ValueError):
			pass
		finally:
			with contextlib.suppress(Exception):
				process.stderr.close()

	def _dispatch(self, event: Dict[str, Any]) -> None:
		if event["type"] == "ready":
			if event.get("protocol") != PROTOCOL_VERSION:
				self._note("the worker speaks protocol %s, this service %s" % (event.get("protocol"), PROTOCOL_VERSION))
			return
		with self._lock:
			pending = self._pending.get(event["id"])
		if pending is not None:
			pending.events.put(event)

	def _ended(self, process: Any, generation: int) -> None:
		with self._lock:
			if self._process is process:
				self._process = None
			waiting = [pending for pending in self._pending.values() if pending.generation == generation]
		for pending in waiting:
			pending.events.put(_DIED)
		self._close_stdin(process)   # the pipes of a worker that is gone are not kept open

	def _close_stdin(self, process: Any) -> None:
		with self._writer:
			with contextlib.suppress(OSError, ValueError):
				process.stdin.close()

	def _terminate(self, process: Any) -> None:
		"""Make sure the worker is gone: by container name first (ending the compose client alone leaves the container running)."""
		if self.container_name:
			self._stop_container(self.container_name)
		if process.poll() is None:
			with contextlib.suppress(OSError):
				process.terminate()
			try:
				process.wait(timeout=3)
			except subprocess.TimeoutExpired:
				with contextlib.suppress(OSError):
					process.kill()
				with contextlib.suppress(subprocess.TimeoutExpired):
					process.wait(timeout=3)

	# ------------------------------------------------------------------ requests
	def _request(self, request: Union[Request, Mapping[str, Any]], action: str) -> Request:
		parsed = request if isinstance(request, Request) else parse_request(request, models=self.models)
		if parsed.action != action:
			raise ValueError("expected a %s request" % action)
		return parsed

	def _exchange(self, request: Request, on_event: Optional[EventCallback], deadline: float, spawn: bool) -> Optional[Dict[str, Any]]:
		if spawn:
			process = self._ensure_process()
		else:
			with self._lock:
				process = self._process
			if process is None or process.poll() is not None:
				return None
		with self._lock:
			pending = _Pending(self._generation)
			self._pending[request.id] = pending
		reported = False
		forwarded_output = False
		try:
			line = (json.dumps(request.to_wire(), ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
			if len(line) > MAX_LINE_BYTES:
				raise ValueError("the request is too large")
			try:
				with self._writer:
					process.stdin.write(line)
					process.stdin.flush()
			except (OSError, ValueError):
				raise _WorkerDied(False)
			while True:
				remaining = deadline - time.monotonic()
				if remaining <= 0:
					self._terminate(process)
					raise AgentTimeout("the question took too long")
				try:
					event = pending.events.get(timeout=min(self.idle_timeout, remaining))
				except queue.Empty:
					self._terminate(process)
					raise AgentTimeout("the agent worker stopped answering")
				if event is _DIED:
					raise _WorkerDied(forwarded_output)
				kind = event["type"]
				if kind == "complete":
					return event
				if kind == "error":
					raise AgentRequestFailed(event["message"], event["code"])
				if kind == "delta":
					forwarded_output = True
				if on_event is not None:
					try:
						on_event(event)
					except Exception as error:   # a caller that went away must not lose the answer: the request goes on
						if not reported:
							reported = True
							self._note("an event callback failed (%s); the request goes on" % type(error).__name__)
		finally:
			with self._lock:
				self._pending.pop(request.id, None)

	def _run(self, request: Request, on_event: Optional[EventCallback] = None, spawn: bool = True) -> Optional[Dict[str, Any]]:
		with self._turn():
			deadline = time.monotonic() + self.request_timeout
			for attempt in (1, 2):
				try:
					return self._exchange(request, on_event, deadline, spawn)
				except _WorkerDied as died:
					if self._stopping:
						raise AgentUnavailable("the replay service is shutting down")
					if not spawn:
						return None
					if died.after_output or attempt == 2:
						raise AgentCrashed("the agent worker stopped while answering")
					self._note("the worker died before answering; starting it again")
		return None   # not reached

	def ask(self, request: Union[Request, Mapping[str, Any]], on_event: Optional[EventCallback] = None) -> Dict[str, Any]:
		"""Ask one question; `on_event` receives the validated progress and delta events; returns the `complete` event.

		Raises RequestError (invalid request, no worker is started for it), AgentRequestFailed (the worker reported an error),
		AgentCrashed, AgentTimeout or AgentUnavailable.
		"""
		return self._run(self._request(request, "ask"), on_event)

	def inspect(self, request: Union[Request, Mapping[str, Any]]) -> Dict[str, Any]:
		"""Whether a staged graph can answer questions: {"ready", "reason", "provenance"}. No provider client is involved."""
		return self._run(self._request(request, "inspect"))["capability"]

	def invalidate(self, result_key: str) -> bool:
		"""Drop the cached graph and Agents of a result in the running worker. False when there is no worker (nothing is cached)."""
		with self._lock:
			if self._stopping or self._process is None:
				return False
		request = parse_request({"id": "inv-" + secrets.token_hex(4), "action": "invalidate", "result_key": result_key})
		try:
			return self._run(request, spawn=False) is not None
		except AgentError:
			return False

	# ------------------------------------------------------------------ ending
	def release_if_idle(self) -> bool:
		"""Stop the worker when no question runs or waits, so its models leave the GPU; the next question starts it again."""
		with self._cond:
			if self._stopping or self._busy or self._waiting or self._process is None:
				return False
			self._busy = True   # hold the turn: nothing may start a worker while this one ends
			process = self._process
		try:
			self._close_stdin(process)
			try:
				process.wait(timeout=self.stop_grace)
			except subprocess.TimeoutExpired:
				self._terminate(process)
		finally:
			with self._cond:
				self._busy = False
				self._cond.notify_all()
		return True

	def stop(self) -> None:
		"""End the service's side: queued requests fail, the worker's input is closed, and a worker that does not end within
		`stop_grace` seconds is stopped by name. Harmless when repeated."""
		with self._cond:
			self._stopping = True
			self._cond.notify_all()
			process = self._process
			threads = list(self._threads)
		if process is not None:
			self._close_stdin(process)
			try:
				process.wait(timeout=self.stop_grace)
			except subprocess.TimeoutExpired:
				self._terminate(process)
		for thread in threads:
			thread.join(timeout=2)


# ---------------------------------------------------------------------------------------------- staging
class StagingError(ValueError):
	"""A graph snapshot that cannot be staged; the message names no host path."""


class StagingChanged(StagingError):
	"""The file is no longer the one that was validated (it was replaced, e.g. by a hot reload): look at the new version."""


_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_SUFFIXES = (".json", ".background.yaml")
DEFAULT_CACHE_BYTES = 2 * 1024 ** 3
_TEMPORARY_AGE = 3600


def _mtime(path: Path) -> float:
	try:
		return path.stat().st_mtime
	except OSError:
		return 0.0


def _remove(path: Path) -> None:
	with contextlib.suppress(OSError):
		path.unlink()


def _prune(cache_root: Path, directory: Path, keep: int, max_bytes: int, protect: Path) -> None:
	"""Leftovers of a crash go; of each kind of file the newest `keep` of a result stay; then the whole cache is brought under
	`max_bytes` by dropping the least recently used files, never the one just staged."""
	now = time.time()
	entries = [entry for entry in directory.iterdir() if entry.is_file() and not entry.is_symlink()]
	for entry in entries:
		if entry.name.startswith(".tmp-") and now - _mtime(entry) > _TEMPORARY_AGE:
			_remove(entry)
	for kind in (".json", ".yaml"):
		files = sorted((entry for entry in entries if entry.name.endswith(kind)), key=_mtime, reverse=True)
		for old in files[keep:]:
			if old != protect:
				_remove(old)
	files = []
	for result in cache_root.iterdir():
		if result.is_dir() and not result.is_symlink():
			files.extend(entry for entry in result.iterdir() if entry.is_file() and not entry.is_symlink() and not entry.name.startswith(".tmp-"))
	total = sum(_size(entry) for entry in files)
	for entry in sorted(files, key=_mtime):
		if total <= max_bytes:
			break
		if entry != protect:
			total -= _size(entry)
			_remove(entry)
	for result in cache_root.iterdir():
		if result.is_dir() and not result.is_symlink():
			with contextlib.suppress(OSError):
				result.rmdir()   # only when empty


def _size(path: Path) -> int:
	try:
		return path.stat().st_size
	except OSError:
		return 0


def stage_file(repo_root: Path, history_key: str, source: Path, digest: str, suffix: str = ".json", keep: int = 2,
		max_bytes: int = DEFAULT_CACHE_BYTES) -> str:
	"""Copy a validated file to `.replay-web/cache/<history key>/<digest><suffix>` for the worker and return that path relative
	to the repository root (the container sees the repository at the same relative path).

	The copy is made to a temporary name, checked against `digest` (the file must be the one that was validated, not a later
	hot-reloaded one) and renamed into place, so a snapshot is whole or absent. A snapshot that exists is reused. Each result keeps
	its newest `keep` graphs and background files, and the cache stays within `max_bytes`.
	"""
	if not _KEY.fullmatch(str(history_key)) or not _DIGEST.fullmatch(str(digest)) or suffix not in _SUFFIXES:
		raise StagingError("the cache key or digest is not valid")
	cache_root = Path(repo_root) / ".replay-web" / "cache"
	directory = cache_root / history_key
	if os.path.islink(str(cache_root)) or os.path.islink(str(directory)):
		raise StagingError("the cache directory is a link")
	final = directory / (digest + suffix)
	relative = "%s/%s/%s%s" % (CACHE_DIR, history_key, digest, suffix)
	os.makedirs(str(directory), mode=0o700, exist_ok=True)
	if final.is_file() and not final.is_symlink():
		os.utime(str(final), None)
		_prune(cache_root, directory, keep, max_bytes, final)
		return relative
	temporary = directory / (".tmp-%d-%s" % (os.getpid(), secrets.token_hex(4)))
	hasher = hashlib.sha256()
	try:
		with open(str(source), "rb") as reader:
			descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
			with os.fdopen(descriptor, "wb") as writer:
				for chunk in iter(lambda: reader.read(1 << 20), b""):
					hasher.update(chunk)
					writer.write(chunk)
				writer.flush()
				os.fsync(writer.fileno())
		if hasher.hexdigest() != digest:
			raise StagingChanged("the file changed after it was validated")
		os.replace(str(temporary), str(final))
	except OSError:
		_remove(temporary)
		raise StagingError("the file could not be copied")
	except BaseException:
		_remove(temporary)
		raise
	_prune(cache_root, directory, keep, max_bytes, final)
	return relative
