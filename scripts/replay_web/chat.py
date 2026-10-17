"""The replay chat of the service: a persistent conversation per open result, answered by the shared Agent worker.

A question is one *turn*. `ChatManager.begin` does everything that can still be refused with a plain error response (the
request, the credentials, whether the result can be asked at all, the scene graph the question is asked against, its staging
for the worker, the earlier conversation) and then records the question; `Turn.run` streams the worker's events and records the
answer. The order is fixed:

1. validate the question, the model (an allowlist) and the replay view that may accompany it;
2. capture the result's active, validated scene graph once, stage that exact version for the worker (digest checked) and keep
   its version with the question, so a hot reload during the turn only affects later turns;
3. take the bounded earlier conversation (only answered questions) and add a visible boundary when the graph version differs from
   the one the conversation last used;
4. store the question; stream the worker's progress and answer;
5. store only the answer text as the assistant message, then announce it; on a failure store a retryable error row instead and
   never an invented answer.

If the page goes away mid-turn the worker request still finishes and its answer is stored, so a reloaded page shows it. One
question per result runs at a time; the worker itself answers one question at a time for all results.

Nothing private is stored or streamed: the worker's events are already reduced to answer text, iteration numbers and tool names
(agent_protocol.py), and failures are described by fixed messages, never by an exception's text. The service does not import the
scene graph library or any model library; readiness comes from the normalized graph document the monitor already holds, and the
worker checks the graph's embedding provenance itself when a question is asked. Standard library only (Python 3.8).
"""

import hashlib
import os
import re
import secrets
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from scripts.replay_web.agent_client import (
	AgentCrashed, AgentError, AgentRequestFailed, AgentTimeout, AgentUnavailable, StagingChanged, StagingError, stage_file,
)
from scripts.replay_web.agent_protocol import (
	MAX_QUESTION_BYTES, MAX_QUESTION_CHARS, Request, RequestError, allowed_models, parse_request, public_error, redact,
)
from scripts.replay_web.artifacts import ActiveGraph
from scripts.replay_web.history import ChatMessage, ConversationStore, GraphRef, HistoryError, history_key, remove_stale_trash, sanitize_context
from scripts.replay_web.runs import OpenRun

CREDENTIALS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
CONVERSATION_BYTES = 56000   # of the 64 KiB the Agent accepts as conversation and replay view together
BODY_FIELDS = frozenset(("question", "model", "include_view_context", "view_context"))
_ENTITY = re.compile(r"[A-Za-z][0-9]{1,9}")
_STATUS_OF_WORKER_ERROR = {
	"invalid-request": 400, "query-not-ready": 409, "graph-missing": 409, "no-credentials": 503, "provider-auth": 503,
	"provider-rate-limit": 429, "provider-unreachable": 502, "model-unavailable": 502,
}


class ChatError(Exception):
	"""A question that cannot be answered. `status` and `code` become the HTTP response; `message` is safe to show."""

	def __init__(self, status: int, message: str, code: str):
		super().__init__(message)
		self.status, self.message, self.code = status, message, code


def _log(message: str) -> None:
	"""A line for the operator's terminal, with anything shaped like an API key masked (a provider's error text may carry one)."""
	sys.stderr.write(redact(message))


def provider_of(model: str) -> str:
	return "anthropic" if model.startswith("claude") else "openai"


def has_credential(repo_root: Path, variable: str, environ: Optional[Mapping[str, str]] = None) -> bool:
	"""Whether an API key is set in the environment or in the repository's .env. Only its presence is looked at, never its value."""
	if (environ if environ is not None else os.environ).get(variable):
		return True
	try:
		for line in (Path(repo_root) / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
			if line.startswith(variable + "=") and len(line.rstrip("\r\n")) > len(variable) + 1:
				return True
	except OSError:
		pass
	return False


def query_state(active: Optional[ActiveGraph]) -> Dict[str, Any]:
	"""Whether the active scene graph can be asked questions: it carries sentence embeddings (Postprocess adds them)."""
	if active is None:
		return {"ready": False, "reason": "this result has no scene graph yet", "graph": None}
	graph = {"name": active.version.name, "token": active.version.token}
	embeddings = active.document.get("embeddings") if hasattr(active.document, "get") else None
	embeddings = embeddings if isinstance(embeddings, Mapping) else {}
	if not embeddings.get("sentence"):
		return {"ready": False, "reason": "the scene graph has no sentence embeddings: run Postprocess first", "graph": graph}
	if not embeddings.get("objects"):
		return {"ready": False, "reason": "no object of the scene graph has a sentence embedding: run Postprocess first", "graph": graph}
	return {"ready": True, "reason": None, "graph": graph}


def _line(text: Any, limit: int = 300) -> str:
	text = " ".join(str(text or "").split())
	return text if len(text) <= limit else text[:limit - 1] + "…"


def _position(item: Mapping[str, Any]) -> str:
	position = item.get("position")
	return " (position %.2f, %.2f, %.2f)" % tuple(position) if isinstance(position, list) and len(position) == 3 else ""


def describe_entity(document: Mapping[str, Any], entity: str) -> Optional[str]:
	"""A sentence about a selected entity taken from the validated graph document, or None when the graph has no such entity.

	The page sends only the id; the words come from the graph, never from the request."""
	if not isinstance(entity, str) or not _ENTITY.fullmatch(entity):
		return None
	kind, number = entity[0], int(entity[1:])
	lists = {"O": "objects", "p": "places", "t": "native_places", "R": "rooms"}
	items = document.get(lists.get(kind, "")) if hasattr(document, "get") else None
	item = next((candidate for candidate in items or () if isinstance(candidate, Mapping) and candidate.get("id") == entity), None)
	if item is None:
		return None
	if kind == "O":
		return "object %d: %s%s" % (number, _line(item.get("description")) or "no description", _position(item))
	if kind == "p":
		return "place %d%s%s" % (number, " in room %s" % item["room"] if item.get("room") else "", _position(item))
	if kind == "t":
		return "semantic place %s: %s%s" % (entity, _line(item.get("description")) or "no description", _position(item))
	label = ' "%s"' % _line(item.get("label"), 80) if item.get("label") else ""
	return "room %s%s: %s" % (entity, label, _line(item.get("description")) or "no summary")


class _Stream:
	"""What `Turn.run` sends to the page. A page that went away must not stop the turn: send failures are swallowed."""

	def __init__(self, emit: Callable[[str, Dict[str, Any]], None]):
		self.emit = emit
		self.started = False   # something was offered to the page, so a plain error response is no longer possible
		self.broken = False

	def send(self, name: str, data: Dict[str, Any]) -> None:
		self.started = True
		if self.broken:
			return
		try:
			self.emit(name, data)
		except Exception:
			self.broken = True


class ChatManager:
	"""The conversations of the open results and the questions asked in them."""

	def __init__(self, repo_root: Path, state_root: Path, registry: Any, monitor: Any, agent: Any, *, models: Optional[Sequence[str]] = None,
			environ: Optional[Mapping[str, str]] = None, publish: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
			enabled: bool = True, stage_root: Optional[Path] = None):
		self.repo_root = Path(repo_root)   # where the API keys are looked for (.env)
		self.stage_root = Path(stage_root) if stage_root is not None else self.repo_root   # the worker sees the repository at the same relative paths
		self.history_root = Path(state_root) / "history"
		self.registry = registry
		self.monitor = monitor
		self.agent = agent
		self.models = tuple(models) if models else allowed_models(environ)
		self.environ = environ if environ is not None else os.environ
		self._publish_event = publish
		self.enabled = enabled
		self._stopped = False
		self._lock = threading.Lock()
		self._stores: Dict[str, ConversationStore] = {}
		self._turns: Dict[str, str] = {}   # history key -> id of the turn that is running
		remove_stale_trash(self.history_root)

	# ------------------------------------------------------------------ what the page may know
	def _credential(self, model: str) -> bool:
		return has_credential(self.repo_root, CREDENTIALS[provider_of(model)], self.environ)

	def agent_payload(self) -> Dict[str, Any]:
		return {
			"enabled": self.enabled and not self._stopped,
			"models": [{"name": model, "provider": provider_of(model), "available": self._credential(model)} for model in self.models],
			"default_model": self.models[0],
			"max_question_chars": MAX_QUESTION_CHARS,
		}

	def config_payload(self) -> Dict[str, Any]:
		"""For /api/config: the allowlist and whether each provider has credentials (never a key)."""
		return {"agent_models": list(self.models), "default_agent_model": self.models[0], "agent": self.agent_payload()}

	def state(self, run_id: str) -> Dict[str, Any]:
		"""What changes between questions: whether a question runs and whether the active graph can answer one."""
		try:
			key = history_key(self.registry.get(run_id).canonical_path)
		except KeyError:
			return {"busy": False, "query": query_state(None)}
		with self._lock:
			busy = key in self._turns
		return {"busy": busy, "query": query_state(self.monitor.active(run_id))}

	def history(self, run: OpenRun) -> Dict[str, Any]:
		try:
			messages = [message.to_json() for message in self._store(run).messages()]
		except HistoryError as error:
			raise ChatError(500, "the conversation history cannot be read: %s" % error, "history")
		state = self.state(run.run_id)
		return {"messages": messages, "busy": state["busy"], "query": state["query"], "agent": self.agent_payload()}

	def _store(self, run: OpenRun) -> ConversationStore:
		key = history_key(run.canonical_path)
		with self._lock:
			store = self._stores.get(key)
			if store is None:
				store = self._stores[key] = ConversationStore(self.history_root, run.canonical_path)
			return store

	def _publish(self, run_id: str, payload: Dict[str, Any]) -> None:
		if self._publish_event is None:
			return
		try:
			self._publish_event(run_id, "chat", payload)
		except Exception:
			_log("[replay-web] a chat event could not be published:\n%s" % traceback.format_exc())

	# ------------------------------------------------------------------ one question
	def _body(self, body: Any) -> Tuple[str, str, bool, Optional[Dict[str, Any]]]:
		if not isinstance(body, dict) or set(body) - BODY_FIELDS:
			raise ChatError(400, 'send {"question", "model", "include_view_context", "view_context"}', "request")
		question = body.get("question")
		if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
			raise ChatError(400, "the question must be text of 1 to %d characters" % MAX_QUESTION_CHARS, "question")
		try:
			if len(question.encode("utf-8")) > MAX_QUESTION_BYTES:
				raise ChatError(400, "the question is too long", "question")
		except UnicodeEncodeError:
			raise ChatError(400, "the question is not valid text", "question")
		model = body.get("model", self.models[0])
		if not isinstance(model, str) or model not in self.models:
			raise ChatError(400, "this model is not available: use one of %s" % ", ".join(self.models), "model")
		include = body.get("include_view_context", True)
		view = body.get("view_context")
		if not isinstance(include, bool) or (view is not None and not isinstance(view, dict)):
			raise ChatError(400, "include_view_context is true or false and view_context an object", "request")
		return question, model, include, view

	@staticmethod
	def _view(document: Mapping[str, Any], raw: Optional[Dict[str, Any]], include: bool) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
		"""(the view stored with the question, the view the Agent is told). The selected entity is kept only when the graph has
		it, and the Agent is told what the graph says about it instead of the bare id."""
		if not include or raw is None:
			return None, {}
		stored = sanitize_context(raw) or {}
		told = dict(stored)
		if "selected_entity" in stored:
			description = describe_entity(document, stored["selected_entity"])
			if description is None:
				del stored["selected_entity"], told["selected_entity"]
			else:
				told["selected_entity"] = description
		return stored or None, told

	def begin(self, run: OpenRun, body: Any) -> "Turn":
		"""Validate and record a question; the returned turn streams its answer. Raises ChatError, storing nothing, whenever the
		question cannot be answered; the result is free again then."""
		if not self.enabled or self._stopped:
			raise ChatError(503, "the replay chat is switched off" if not self._stopped else "the replay service is shutting down", "disabled")
		question, model, include, raw_view = self._body(body)
		variable = CREDENTIALS[provider_of(model)]
		if not self._credential(model):
			raise ChatError(503, "no %s is set: put it into the .env file of the repository (the key is only read by the replay worker)" % variable, "no-credentials")
		key, turn_id = history_key(run.canonical_path), secrets.token_hex(6)
		with self._lock:
			if key in self._turns:
				raise ChatError(409, "another question of this result is still being answered", "busy")
			self._turns[key] = turn_id
		try:
			return self._start(run, key, turn_id, question, model, include, raw_view)
		except BaseException:
			with self._lock:
				self._turns.pop(key, None)
			raise

	def _active(self, run: OpenRun) -> ActiveGraph:
		active = self.monitor.active(run.run_id)
		if active is None:
			raise ChatError(409, "this result has no scene graph yet", "no-graph")
		state = query_state(active)
		if not state["ready"]:
			raise ChatError(409, state["reason"], "not-ready")
		return active

	def _stage(self, run: OpenRun, key: str) -> Tuple[ActiveGraph, str, Optional[str], Optional[str]]:
		"""The active graph and its snapshot (and, for CODa, the background file) staged for the worker. A graph replaced since it was
		validated is looked at once more; one that keeps changing is refused."""
		active = self._active(run)
		for attempt in (1, 2):
			try:
				graph_path = stage_file(self.stage_root, key, active.version.path, active.version.digest)
				break
			except StagingChanged:
				if attempt == 2:
					raise ChatError(409, "the scene graph is being replaced: ask again in a moment", "graph-changing")
				active = self._active(run)
			except StagingError as error:
				raise ChatError(503, "the scene graph could not be prepared for the worker (%s)" % error, "staging")
		background_path, suffix = None, None
		if run.manifest["dataset"]["kind"] == "coda":
			source = run.canonical_path / "background_objects.yaml"
			try:
				data = source.read_bytes() if source.is_file() else None
			except OSError:
				data = None
			if data is not None:
				digest = hashlib.sha256(data).hexdigest()
				try:
					background_path = stage_file(self.stage_root, key, source, digest, suffix=".background.yaml")
				except StagingError as error:
					raise ChatError(503, "the background objects could not be prepared for the worker (%s)" % error, "staging")
				suffix = digest[:12]
		return active, graph_path, background_path, suffix

	def _start(self, run: OpenRun, key: str, turn_id: str, question: str, model: str, include: bool, raw_view: Optional[Dict[str, Any]]) -> "Turn":
		active, graph_path, background_path, suffix = self._stage(run, key)
		store = self._store(run)
		stored_view, told_view = self._view(active.document, raw_view, include)
		reference = GraphRef(active.version.name, active.version.token)
		try:
			conversation = store.context_messages(max_bytes=CONVERSATION_BYTES)
		except HistoryError as error:
			raise ChatError(500, "the conversation history cannot be read: %s" % error, "history")
		dataset = run.manifest["dataset"]
		wire: Dict[str, Any] = {
			"id": "turn-" + turn_id, "action": "ask", "result_key": key, "graph_path": graph_path,
			"graph_version": active.version.token + (":" + suffix if suffix else ""),
			"dataset": {"kind": dataset["kind"], "sequence": dataset["sequence"], "time_origin": dataset["time_origin"]},
			"model": model, "question": question, "conversation": conversation, "context": told_view,
		}
		if background_path is not None:
			wire["background_path"] = background_path
		try:
			request = parse_request(wire, models=self.models)
		except RequestError as error:
			raise ChatError(400, str(error), "request")
		try:
			boundary = store.append_boundary(reference)
			user = store.append_user(question, model=model, graph=reference, context=stored_view)
		except HistoryError as error:
			raise ChatError(400, str(error), "question")
		self._publish(run.run_id, {"busy": True, "turn": turn_id})
		return Turn(self, run, key, turn_id, store, request, reference, model, user, boundary)

	def _finish(self, turn: "Turn") -> None:
		with self._lock:
			if self._turns.get(turn.key) == turn.turn_id:
				del self._turns[turn.key]
		self._publish(turn.result.run_id, {"busy": False, "turn": turn.turn_id})

	# ------------------------------------------------------------------ the rest of the conversation's life
	def clear(self, run: OpenRun) -> None:
		"""Forget this result's conversation (other results keep theirs); refused while one of its questions is being answered."""
		store = self._store(run)
		with self._lock:
			if store.key in self._turns:
				raise ChatError(409, "a question of this result is still being answered", "busy")
		try:
			store.clear()
		except (HistoryError, OSError) as error:
			raise ChatError(500, "the conversation could not be cleared (%s)" % (error.strerror if isinstance(error, OSError) else error), "history")

	def forget(self, run: OpenRun) -> Optional[threading.Thread]:
		"""A result was closed: let the worker drop its cached graph. Its history stays on disk. The worker answers one request at
		a time, so this waits for its turn in a thread of its own rather than holding up the request that closed the result."""
		key = history_key(run.canonical_path)
		with self._lock:
			self._stores.pop(key, None)

		def invalidate() -> None:
			try:
				self.agent.invalidate(key)
			except Exception:
				_log("[replay-web] the agent worker could not drop a closed result:\n%s" % traceback.format_exc())

		thread = threading.Thread(target=invalidate, name="chat-forget", daemon=True)
		thread.start()
		return thread

	def release_for_job(self) -> None:
		"""A processing job is about to start: an idle worker gives its models back to the GPU (the next question starts it again)."""
		try:
			self.agent.release_if_idle()
		except Exception:
			_log("[replay-web] the idle agent worker could not be released:\n%s" % traceback.format_exc())

	def stop(self) -> None:
		self._stopped = True
		try:
			self.agent.stop()
		except Exception:
			_log("[replay-web] the agent worker could not be stopped cleanly:\n%s" % traceback.format_exc())


class _Refusal(Exception):
	"""A failure of the turn itself (not of the worker) with its own fixed description."""

	def __init__(self, status: int, code: str, message: str):
		super().__init__(message)
		self.status, self.code, self.message = status, code, message


class Turn:
	"""One recorded question, ready to be answered. `run` streams the answer; `release` frees the result for a turn never run."""

	def __init__(self, manager: ChatManager, result: OpenRun, key: str, turn_id: str, store: ConversationStore, request: Request,
			reference: GraphRef, model: str, user: ChatMessage, boundary: Optional[ChatMessage]):
		self.manager, self.result, self.key, self.turn_id, self.store = manager, result, key, turn_id, store
		self.request, self.reference, self.model, self.user, self.boundary = request, reference, model, user, boundary
		self._done = False

	def release(self) -> None:
		if not self._done:
			self._done = True
			self.manager._finish(self)

	def run(self, emit: Callable[[str, Dict[str, Any]], None]) -> None:
		"""Answer the question, streaming through `emit(name, data)`: `turn` (the stored question), `progress`, `delta`, then
		`complete` or `error`. A failure is stored as an error row. It raises ChatError (after storing that row) only when nothing
		has been streamed yet, because the worker could not be reached: the caller then still has a plain response to give."""
		try:
			self._answer(_Stream(emit))
		finally:
			self.release()

	def _graph(self) -> Dict[str, str]:
		return {"name": self.reference.name, "version": self.reference.version}

	def _answer(self, stream: _Stream) -> None:
		agent = self.manager.agent
		pending: List[Tuple[str, Dict[str, Any]]] = [("turn", {
			"turn": self.turn_id, "user": self.user.to_json(), "boundary": self.boundary.to_json() if self.boundary else None,
			"model": self.model, "graph": self._graph(),
		})]

		def flush() -> None:   # the stored question goes first, but only once there is something to follow it
			while pending:
				stream.send(*pending.pop(0))

		def forward(event: Mapping[str, Any]) -> None:
			flush()
			if event["type"] == "progress":
				stream.send("progress", dict({key: value for key, value in event.items() if key not in ("id", "type")}, turn=self.turn_id))
			elif event["type"] == "delta":
				stream.send("delta", {"turn": self.turn_id, "text": event["text"]})

		try:
			if getattr(agent, "busy", False) or getattr(agent, "queued", 0):   # another question is being answered: wait in line, visibly
				flush()
				stream.send("progress", {"turn": self.turn_id, "kind": "queued", "ahead": getattr(agent, "queued", 0) + 1})
			answer = agent.ask(self.request, forward)["answer"]
			if not isinstance(answer, str) or not answer.strip():
				raise _Refusal(500, "query-failed", "The model returned an empty answer.")
			assistant = self.store.append_assistant(answer, model=self.model, graph=self.reference)
			flush()
			stream.send("complete", {"turn": self.turn_id, "message": assistant.to_json(), "model": self.model, "graph": self._graph()})
		except Exception as error:
			status, code, message = self._explain(error)
			try:
				row = self.store.append_error(message, model=self.model, graph=self.reference, code=code)
			except HistoryError:
				row = None
			if not stream.started:
				raise ChatError(status, message, code)
			flush()
			stream.send("error", {"turn": self.turn_id, "code": code, "message": message, "retryable": True, "row": row.to_json() if row else None})

	@staticmethod
	def _explain(error: BaseException) -> Tuple[int, str, str]:
		"""(HTTP status for when nothing was streamed yet, code, message) of a failure: fixed texts, never the exception's own."""
		if isinstance(error, _Refusal):
			return error.status, error.code, error.message
		if isinstance(error, AgentRequestFailed):
			return _STATUS_OF_WORKER_ERROR.get(error.code, 502), error.code, error.message
		if isinstance(error, AgentTimeout):
			return 504, "timeout", error.message
		if isinstance(error, AgentUnavailable):
			return 503, "worker-unavailable", error.message
		if isinstance(error, AgentCrashed):
			return 503, "worker-crashed", error.message
		if isinstance(error, AgentError):
			return 502, error.code, error.message
		_log("[replay-web] a question failed unexpectedly:\n%s" % "".join(traceback.format_exception(type(error), error, error.__traceback__)))
		code, message = public_error(error)
		return 500, "query-failed" if code == "invalid-request" else code, message
