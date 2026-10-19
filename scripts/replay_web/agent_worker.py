"""The replay Agent worker: one long-lived process in the daaam-ws container that answers scene questions.

The replay service starts it once (`docker compose run ... python -m scripts.replay_web.agent_worker`) and talks to it over
stdin/stdout in the NDJSON protocol of agent_protocol.py: every tab and every result shares this one process, which keeps the
sentence/CLIP models and the loaded scene graphs in memory (AgentCache) and answers one request at a time. It reads graphs only
from the staged snapshots under `.replay-web/cache/`, never touches a job, and ends by itself at end of input or after being idle
for a while (so the GPU and memory are free for the next pipeline or post-processing run).

Standard output carries nothing but protocol events: at start it is moved to stderr for everything else (Python's `print`, the
libraries' C++ output), and the protocol writes to a private copy of the original descriptor. API keys come from the container's
environment or the repository's `.env` (loaded by `import daaam`), are only checked for presence and never leave this process.
"""

import argparse
import contextlib
import os
import queue
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from scripts.replay_web.agent_protocol import (
	CACHE_DIR, MAX_ANSWER_BYTES, MAX_LINE_BYTES, Request, RequestError, allowed_models, complete_event,
	delta_event, encode_event, error_event, parse_line, progress_event, public_error, ready_event, redact, text_chunks,
)
from scripts.replay_web.history import clip_message
from scripts.replay_web.query import AgentCache, query_capability

DEFAULT_IDLE_SECONDS = 900.0
IDLE_ENV = "REPLAY_AGENT_IDLE_SECONDS"
Emit = Callable[[Mapping[str, Any]], None]


class PublicError(Exception):
	"""A failure the worker describes itself: its code and message are safe to send."""

	def __init__(self, code: str, message: str):
		super().__init__(message)
		self.code, self.message = code, message


def credential_variable(model: str) -> str:
	"""The environment variable holding the key of the provider a model name selects (as detect_provider does)."""
	return "ANTHROPIC_API_KEY" if model.startswith("claude") else "OPENAI_API_KEY"


class Worker:
	"""Answers requests one at a time. Everything it needs from outside is injectable, so that it runs without models."""

	def __init__(self, repo_root: Path, cache: Optional[AgentCache] = None, environ: Optional[Mapping[str, str]] = None,
			response_format: Any = None, models: Optional[Sequence[str]] = None, log: Optional[Callable[[str], None]] = None):
		self.repo_root = Path(repo_root)
		self.cache = cache if cache is not None else AgentCache(max_graphs=2)
		self.environ = environ if environ is not None else os.environ
		self.response_format = response_format
		self.models = tuple(models) if models is not None else allowed_models(self.environ)
		write = log or (lambda message: sys.stderr.write("[agent] %s\n" % message))
		self._log = lambda message: write(redact(message, [self.environ.get(name, "") for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")]))

	# -------------------------------------------------------------- one request
	def handle_line(self, line: Any, emit: Emit) -> None:
		try:
			request = parse_line(line, models=self.models)
		except RequestError as error:
			emit(error_event(error.request_id, "invalid-request", str(error)[:300]))
			return
		self.handle(request, emit)

	def handle(self, request: Request, emit: Emit) -> None:
		"""Run one request. Whatever fails becomes an `error` event: one question never ends the worker."""
		try:
			with contextlib.redirect_stdout(sys.stderr):
				if request.action == "ask":
					self._ask(request, emit)
				elif request.action == "inspect":
					self._inspect(request, emit)
				else:
					self.cache.invalidate(request.result_key)
					emit(complete_event(request.id))
		except PublicError as error:
			emit(error_event(request.id, error.code, error.message))
		except Exception as error:
			self._log("%s failed: %s" % (request.action, "".join(traceback.format_exception_only(type(error), error)).strip()))
			if self.environ.get("REPLAY_AGENT_TRACE"):
				traceback.print_exc()
			emit(error_event(request.id, *public_error(error)))

	def _resolve(self, relative: str) -> Path:
		"""The staged snapshot a request names: an existing file that really lies in the cache directory (links are followed)."""
		base = (self.repo_root / CACHE_DIR).resolve()
		try:
			path = (self.repo_root / relative).resolve(strict=True)
		except (OSError, RuntimeError):
			raise PublicError("graph-missing", "The scene graph snapshot is no longer available.")
		if base not in path.parents or not path.is_file():
			raise PublicError("graph-missing", "The scene graph snapshot is no longer available.")
		return path

	def _inspect(self, request: Request, emit: Emit) -> None:
		path = self._resolve(request.graph_path)
		emit(complete_event(request.id, capability=query_capability(path).to_json()))

	def _ask(self, request: Request, emit: Emit) -> None:
		variable = credential_variable(request.model)
		if not self.environ.get(variable):
			raise PublicError("no-credentials", "No %s is set for the replay worker: put it into the .env file of the repository." % variable)
		path = self._resolve(request.graph_path)
		background = self._resolve(request.background_path) if request.background_path else None

		def report(progress: Mapping[str, Any]) -> None:
			event = progress_event(request.id, progress)
			if event is not None:
				emit(event)

		report({"type": "loading"})
		agent = self.cache.get(request.result_key, path, request.graph_version, request.dataset, request.model, background_objects=background)
		response_format = self.response_format
		if response_format is None:
			from daaam.scene_understanding.models import TextResponse
			response_format = TextResponse
		answer, _history, _messages = agent.answer_query(
			response_format, request.question, conversation=list(request.conversation), context=dict(request.context), progress=report,
		)
		text = answer.answer   # only this text is sent: the structured answer's reasoning stays here
		if not isinstance(text, str) or not text.strip():
			raise PublicError("query-failed", "The model returned an empty answer.")
		text = clip_message(text, MAX_ANSWER_BYTES)
		for chunk in text_chunks(text):
			emit(delta_event(request.id, chunk))
		emit(complete_event(request.id, answer=text, model=request.model, graph_version=request.graph_version))

	# -------------------------------------------------------------- the loop
	def serve(self, source: "queue.Queue", emit: Emit, idle_seconds: Optional[float] = None) -> int:
		"""Announce readiness, then answer the lines of `source` (a queue; None ends it) until it ends or stays idle."""
		emit(ready_event())
		while True:
			try:
				line = source.get(timeout=idle_seconds) if idle_seconds else source.get()
			except queue.Empty:
				self._log("idle for %g seconds: exiting to free the models" % idle_seconds)
				return 0
			if line is None:
				return 0
			try:
				self.handle_line(line, emit)
			except BrokenPipeError:
				return 1   # the service is gone


# ---------------------------------------------------------------------------------------------- process setup
def _claim_stdout() -> Any:
	"""A private binary stream on the original standard output; descriptor 1 and sys.stdout are pointed at stderr, so that
	nothing else (Python or C++ libraries) can write into the protocol stream."""
	stream = os.fdopen(os.dup(1), "wb")
	sys.stdout.flush()
	os.dup2(2, 1)
	sys.stdout = sys.stderr
	return stream


def _read_requests(stream: Any, target: "queue.Queue") -> None:
	"""Feed the request lines of `stream` to `target`; None marks the end. An over-long line becomes one marker the parser refuses."""
	while True:
		line = stream.readline(MAX_LINE_BYTES + 1)
		if not line:
			target.put(None)
			return
		if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
			while True:
				rest = stream.readline(1 << 16)
				if not rest or rest.endswith(b"\n"):
					break
			line = b"x" * (MAX_LINE_BYTES + 1)
		target.put(line)


def _load_environment() -> None:
	"""Importing daaam loads the repository's .env (python-dotenv), the same way the other container scripts get their keys."""
	try:
		import daaam  # noqa: F401
	except ImportError:
		pass


def main(argv: Optional[Sequence[str]] = None, *, cache: Optional[AgentCache] = None, stdin: Any = None, claim_stdout: bool = True,
		load_environment: Optional[Callable[[], None]] = None, response_format: Any = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
	parser.add_argument("--repo-root", default=".", help="the repository directory (default: the current directory)")
	parser.add_argument("--idle-seconds", type=float, default=None,
		help="exit after this many seconds without a request (0: never; default: $%s or %g)" % (IDLE_ENV, DEFAULT_IDLE_SECONDS))
	args = parser.parse_args(argv)
	idle = args.idle_seconds
	if idle is None:
		try:
			idle = float(os.environ.get(IDLE_ENV, DEFAULT_IDLE_SECONDS))
		except ValueError:
			idle = DEFAULT_IDLE_SECONDS
	(load_environment or _load_environment)()
	output = _claim_stdout() if claim_stdout else sys.stdout.buffer

	def emit(event: Mapping[str, Any]) -> None:
		output.write(encode_event(event).encode("ascii"))
		output.flush()

	source: "queue.Queue" = queue.Queue()
	threading.Thread(target=_read_requests, args=(stdin if stdin is not None else sys.stdin.buffer, source), daemon=True).start()
	worker = Worker(Path(args.repo_root).resolve(), cache=cache, response_format=response_format)
	return worker.serve(source, emit, idle if idle > 0 else None)


def run(argv: Optional[Sequence[str]] = None, **injected: Any) -> None:
	"""Run the worker as the process it is and end the process right away.

	An idle worker ends while the thread that reads its input is still waiting in `readline`; letting the interpreter finalize
	then aborts it ("could not acquire lock ... possibly due to daemon threads"), so the exit status would be a crash.
	Everything the worker owes its peer has been flushed by then.
	"""
	code = 1
	try:
		code = main(argv, **injected)
	finally:
		for stream in (sys.stdout, sys.stderr):
			with contextlib.suppress(Exception):
				stream.flush()
		os._exit(code)


if __name__ == "__main__":
	run()
