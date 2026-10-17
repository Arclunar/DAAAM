"""The NDJSON protocol between the replay service (WSL host) and its Agent worker (one process in the daaam-ws container).

One JSON object per line in each direction. A request is `inspect`, `ask` or `invalidate`; the worker answers with events
carrying the request's id: any number of `progress` and `delta` events, then exactly one `complete` or `error`. Before its first
request the worker says `ready` once (no id).

Both ends validate everything they read, with the same functions, so that nothing but the fixed fields below crosses the
boundary: a worker event can never carry prompts, tool arguments or results, model reasoning, host paths or credentials into the
service, and a request can never name a file outside the staged graph snapshots. The standard library only (Python 3.8).
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence, Tuple

from scripts.replay_web.history import MAX_MESSAGE_BYTES, sanitize_context
from scripts.replay_web.query import DatasetContext, QueryConfigurationError

PROTOCOL_VERSION = 1
CACHE_DIR = ".replay-web/cache"                 # staged graph snapshots, relative to the repository root
DEFAULT_MODEL = "gpt-5-mini"
MODELS_ENV = "REPLAY_AGENT_MODELS"
ACTIONS = ("inspect", "ask", "invalidate")
MAX_LINE_BYTES = 512 * 1024                     # one request line
MAX_EVENT_BYTES = 1024 * 1024                   # one event line
MAX_QUESTION_CHARS = 4000
MAX_QUESTION_BYTES = 16000
MAX_TURNS = 400
MAX_CONVERSATION_BYTES = 64 * 1024              # what SceneUnderstandingAgent accepts as conversation
MAX_ANSWER_BYTES = MAX_MESSAGE_BYTES            # an answer is stored as one history message
MAX_DELTA_CHARS = 4096
MAX_ERROR_CHARS = 500
ERROR_CODES = frozenset((
	"invalid-request", "graph-missing", "query-not-ready", "no-credentials", "provider-auth", "provider-rate-limit",
	"provider-unreachable", "model-unavailable", "query-failed",
))

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}")
_VERSION = re.compile(r"[A-Za-z0-9_.:+-]{1,128}")
_TOOL = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_GRAPH = re.compile(r"\.replay-web/cache/[A-Za-z0-9_-]{1,128}/[A-Za-z0-9_.-]{1,160}\.json")
_BACKGROUND = re.compile(r"\.replay-web/cache/[A-Za-z0-9_-]{1,128}/[A-Za-z0-9_.-]{1,160}\.ya?ml")


def _is_int(value: Any) -> bool:
	return isinstance(value, int) and not isinstance(value, bool)


def allowed_models(environ: Optional[Mapping[str, str]] = None) -> Tuple[str, ...]:
	"""The models a question may name: REPLAY_AGENT_MODELS (comma, semicolon or blank separated), by default only gpt-5-mini.

	Entries that are not plain model names are dropped. The first one is the default.
	"""
	raw = (environ if environ is not None else os.environ).get(MODELS_ENV, "")
	models = []
	for token in re.split(r"[\s,;]+", raw):
		if token and _MODEL.fullmatch(token) and token not in models:
			models.append(token)
	return tuple(models) or (DEFAULT_MODEL,)


# ---------------------------------------------------------------------------------------------- requests
class RequestError(ValueError):
	"""A request that is refused. `request_id` is the request's own id when it is a plain token, else None."""

	def __init__(self, message: str, request_id: Optional[str] = None):
		super().__init__(message)
		self.request_id = request_id


@dataclass(frozen=True)
class Request:
	"""A validated request. Fields an action does not use stay None (or empty)."""
	id: str
	action: str
	result_key: str
	graph_path: Optional[str] = None
	background_path: Optional[str] = None
	graph_version: Optional[str] = None
	dataset: Optional[DatasetContext] = None
	model: Optional[str] = None
	question: Optional[str] = None
	conversation: Tuple[Dict[str, str], ...] = ()
	context: Dict[str, Any] = field(default_factory=dict)

	def to_wire(self) -> Dict[str, Any]:
		"""The JSON form sent to the worker."""
		wire: Dict[str, Any] = {"id": self.id, "action": self.action, "result_key": self.result_key}
		if self.action == "invalidate":
			return wire
		wire.update(graph_path=self.graph_path, graph_version=self.graph_version, dataset={
			"kind": self.dataset.kind, "sequence": self.dataset.sequence, "time_origin": self.dataset.time_origin,
		})
		if self.background_path is not None:
			wire["background_path"] = self.background_path
		if self.action == "ask":
			wire.update(model=self.model, question=self.question, conversation=[dict(turn) for turn in self.conversation], context=dict(self.context))
		return wire


_FIELDS = {
	"invalidate": frozenset(("id", "action", "result_key")),
	"inspect": frozenset(("id", "action", "result_key", "graph_path", "background_path", "graph_version", "dataset")),
	"ask": frozenset(("id", "action", "result_key", "graph_path", "background_path", "graph_version", "dataset", "model", "question",
		"conversation", "context")),
}
_REQUIRED = {
	"invalidate": ("result_key",),
	"inspect": ("result_key", "graph_path", "graph_version", "dataset"),
	"ask": ("result_key", "graph_path", "graph_version", "dataset", "model", "question"),
}


def _token(value: Any, pattern: "re.Pattern", name: str, request_id: Optional[str]) -> str:
	if not isinstance(value, str) or not pattern.fullmatch(value):
		raise RequestError("%s is not valid" % name, request_id)
	return value


def _conversation(value: Any, request_id: Optional[str]) -> Tuple[Dict[str, str], ...]:
	if not isinstance(value, list) or len(value) > MAX_TURNS:
		raise RequestError("the conversation must be a list of at most %d turns" % MAX_TURNS, request_id)
	turns, size = [], 0
	for turn in value:
		role = turn.get("role") if isinstance(turn, dict) else None
		content = turn.get("content") if isinstance(turn, dict) else None
		if role not in ("user", "assistant") or not isinstance(content, str):
			raise RequestError("a conversation turn needs a user/assistant role and text", request_id)
		try:
			size += len(content.encode("utf-8"))
		except UnicodeEncodeError:
			raise RequestError("a conversation turn is not valid Unicode", request_id)
		turns.append({"role": role, "content": content})
	if size > MAX_CONVERSATION_BYTES:
		raise RequestError("the conversation is larger than %d bytes" % MAX_CONVERSATION_BYTES, request_id)
	return tuple(turns)


def _question(value: Any, request_id: Optional[str]) -> str:
	if not isinstance(value, str) or not value.strip() or len(value) > MAX_QUESTION_CHARS:
		raise RequestError("the question must be non-empty text of at most %d characters" % MAX_QUESTION_CHARS, request_id)
	try:
		if len(value.encode("utf-8")) > MAX_QUESTION_BYTES:
			raise RequestError("the question is larger than %d bytes" % MAX_QUESTION_BYTES, request_id)
	except UnicodeEncodeError:
		raise RequestError("the question is not valid Unicode", request_id)
	return value


def parse_request(raw: Any, *, models: Optional[Sequence[str]] = None) -> Request:
	"""Validate a decoded request. With `models`, a question must name one of them. Raises RequestError."""
	request_id = raw.get("id") if isinstance(raw, dict) else None
	request_id = request_id if isinstance(request_id, str) and _ID.fullmatch(request_id) else None
	if not isinstance(raw, dict):
		raise RequestError("a request is a JSON object")
	action = raw.get("action")
	if action not in ACTIONS:
		raise RequestError("unknown action", request_id)
	unknown = sorted(key for key in raw if key not in _FIELDS[action])
	if unknown:
		raise RequestError("unknown field: %s" % str(unknown[0])[:40], request_id)
	missing = [name for name in ("id",) + _REQUIRED[action] if raw.get(name) is None]
	if missing:
		raise RequestError("missing field: %s" % missing[0], request_id)
	identifier = _token(raw["id"], _ID, "id", request_id)
	result_key = _token(raw["result_key"], _KEY, "result_key", request_id)
	if action == "invalidate":
		return Request(identifier, action, result_key)
	graph_path = _token(raw["graph_path"], _GRAPH, "graph_path", request_id)
	graph_version = _token(raw["graph_version"], _VERSION, "graph_version", request_id)
	background = raw.get("background_path")
	if background is not None:
		background = _token(background, _BACKGROUND, "background_path", request_id)
	try:
		dataset = DatasetContext.from_manifest(raw["dataset"])
	except QueryConfigurationError as error:
		raise RequestError("dataset: %s" % error, request_id)
	if action == "inspect":
		return Request(identifier, action, result_key, graph_path, background, graph_version, dataset)
	model = _token(raw["model"], _MODEL, "model", request_id)
	if models is not None and model not in models:
		raise RequestError("the model is not allowed", request_id)
	question = _question(raw["question"], request_id)
	conversation = _conversation(raw.get("conversation", []), request_id)
	context = sanitize_context(raw.get("context")) or {}
	return Request(identifier, action, result_key, graph_path, background, graph_version, dataset, model, question, conversation, context)


def parse_line(line: Any, *, models: Optional[Sequence[str]] = None) -> Request:
	"""Decode and validate one request line (bytes or text)."""
	if len(line) > MAX_LINE_BYTES:
		raise RequestError("the request line is larger than %d bytes" % MAX_LINE_BYTES)
	try:
		text = line.decode("utf-8") if isinstance(line, bytes) else line
		raw = json.loads(text)
	except (UnicodeDecodeError, ValueError):
		raise RequestError("the request line is not JSON")
	return parse_request(raw, models=models)


# ---------------------------------------------------------------------------------------------- events
def encode_event(event: Mapping[str, Any]) -> str:
	"""One line of ASCII JSON ending in a newline: no locale, line-separator or encoding surprises on the pipe."""
	return json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n"


def ready_event() -> Dict[str, Any]:
	return {"id": None, "type": "ready", "protocol": PROTOCOL_VERSION}


def progress_event(request_id: str, progress: Any) -> Optional[Dict[str, Any]]:
	"""The safe form of an Agent progress event, or None. Only iteration numbers and tool names survive: no arguments,
	results, prompts or reasoning, whatever else the event carries."""
	if not isinstance(progress, Mapping):
		return None
	kind = progress.get("type")
	base = {"id": request_id, "type": "progress", "kind": kind}
	if kind in ("final", "loading"):
		return base
	if kind == "tool":
		name = progress.get("name")
		return dict(base, name=name) if isinstance(name, str) and _TOOL.fullmatch(name) else None
	if kind == "iteration":
		number, maximum = progress.get("number"), progress.get("maximum")
		if _is_int(number) and _is_int(maximum) and 1 <= number <= maximum <= 1000:
			return dict(base, number=number, maximum=maximum)
	return None


def delta_event(request_id: str, text: str) -> Dict[str, Any]:
	return {"id": request_id, "type": "delta", "text": text}


def complete_event(request_id: str, **payload: Any) -> Dict[str, Any]:
	return dict(payload, id=request_id, type="complete")


def error_event(request_id: Optional[str], code: str, message: str) -> Dict[str, Any]:
	return {"id": request_id, "type": "error", "code": code, "message": message}


def text_chunks(text: str, size: int = 80) -> Iterator[str]:
	"""The text in pieces of at most `size` characters, for the streamed `delta` events."""
	for start in range(0, len(text), size):
		yield text[start:start + size]


def _encoder_json(raw: Any) -> Optional[Dict[str, Any]]:
	if isinstance(raw, dict):
		model, backend, dim, pretrained = raw.get("model_name"), raw.get("backend"), raw.get("dim"), raw.get("pretrained")
		if (isinstance(model, str) and 0 < len(model) <= 256 and isinstance(backend, str) and 0 < len(backend) <= 64 and _is_int(dim)
				and (pretrained is None or (isinstance(pretrained, str) and len(pretrained) <= 256))):
			return {"model_name": model, "backend": backend, "dim": dim, "pretrained": pretrained}
	return None


def _capability_json(raw: Any) -> Optional[Dict[str, Any]]:
	if not isinstance(raw, dict) or not isinstance(raw.get("ready"), bool):
		return None
	reason = raw.get("reason")
	if reason is not None and not isinstance(reason, str):
		return None
	provenance = raw.get("provenance")
	if provenance is not None:
		sentence = _encoder_json(provenance.get("sentence")) if isinstance(provenance, dict) else None
		clip = provenance.get("clip") if isinstance(provenance, dict) else None
		clip = _encoder_json(clip) if clip is not None else None
		provenance = {"sentence": sentence, "clip": clip} if sentence is not None else None
	return {"ready": raw["ready"], "reason": reason[:MAX_ERROR_CHARS] if reason is not None else None, "provenance": provenance}


def parse_event(line: Any) -> Optional[Dict[str, Any]]:
	"""Validate one line from the worker and rebuild the event from its known fields, or None when it is not a valid event."""
	try:
		text = line.decode("utf-8") if isinstance(line, bytes) else line
		if len(text) > MAX_EVENT_BYTES:
			return None
		raw = json.loads(text)
	except (UnicodeDecodeError, ValueError):
		return None
	if not isinstance(raw, dict):
		return None
	kind, request_id = raw.get("type"), raw.get("id")
	if kind == "ready":
		return {"id": None, "type": "ready", "protocol": raw["protocol"]} if _is_int(raw.get("protocol")) else None
	if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
		return None
	if kind == "progress":
		return progress_event(request_id, dict(raw, type=raw.get("kind")))
	if kind == "delta":
		delta = raw.get("text")
		return delta_event(request_id, delta) if isinstance(delta, str) and len(delta) <= MAX_DELTA_CHARS else None
	if kind == "error":
		code, message = raw.get("code"), raw.get("message")
		return error_event(request_id, code if code in ERROR_CODES else "query-failed", message[:MAX_ERROR_CHARS] if isinstance(message, str) else "")
	if kind != "complete":
		return None
	if "answer" in raw:
		answer, model, version = raw["answer"], raw.get("model"), raw.get("graph_version")
		if (not isinstance(answer, str) or len(answer.encode("utf-8", "replace")) > MAX_ANSWER_BYTES or not isinstance(model, str)
				or not _MODEL.fullmatch(model) or not isinstance(version, str) or not _VERSION.fullmatch(version)):
			return None
		return complete_event(request_id, answer=answer, model=model, graph_version=version)
	if "capability" in raw:
		capability = _capability_json(raw["capability"])
		return complete_event(request_id, capability=capability) if capability is not None else None
	return complete_event(request_id)


# ---------------------------------------------------------------------------------------------- secrets and errors
_SECRETS = (
	(re.compile(r"\bsk-[A-Za-z0-9_-]{6,}"), "sk-***"),
	(re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 ***"),
	(re.compile(r"(?i)((?:api[_-]?key|token|secret|password|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]+"), r"\1***"),
)


def redact(text: str, secrets: Sequence[str] = ()) -> str:
	"""A log line with API keys and credentials masked: the exact values given in `secrets` and anything shaped like a key."""
	for secret in secrets:
		if secret:
			text = text.replace(secret, "***")
	for pattern, replacement in _SECRETS:
		text = pattern.sub(replacement, text)
	return text


def _names(error: BaseException) -> frozenset:
	return frozenset(cls.__name__ for cls in type(error).__mro__)


def public_error(error: BaseException) -> Tuple[str, str]:
	"""A code and a message that are safe to show, for any exception. The exception's own text is never used (a provider's
	message can carry key fragments, and a traceback host paths); a QueryConfigurationError's message is safe by contract."""
	if isinstance(error, QueryConfigurationError):
		return "query-not-ready", str(error)[:300]
	names = _names(error)
	if "NotFoundError" in names:
		return "model-unavailable", "The model is not available for this account."
	if "RateLimitError" in names:
		return "provider-rate-limit", "The model provider is rate limiting requests. Try again in a moment."
	if names & {"APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout", "ReadTimeout", "TimeoutError"}:
		return "provider-unreachable", "The model provider could not be reached."
	if names & {"AuthenticationError", "PermissionDeniedError"} or type(error).__name__ == "OpenAIError":
		return "provider-auth", "The model provider rejected the API credentials, or none are configured for the replay worker."
	if isinstance(error, ValueError):
		return "invalid-request", "The question was rejected as invalid."
	return "query-failed", "The question could not be answered (%s)." % re.sub(r"[^A-Za-z0-9_]", "", type(error).__name__)[:60]
