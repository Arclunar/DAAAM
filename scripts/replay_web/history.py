"""Persistent chat history of the replay workspace: one conversation per result, kept next to the other local state.

A conversation lives in `<root>/<key>/`, the key being the SHA-256 of the result's canonical path, so it survives a restart of
the service or the browser and never mixes with another result. `messages.jsonl` is append-only (one flushed line per message),
`meta.json` holds the schema version, the canonical path, the creation time and, once a long conversation was compacted, the
extractive summary. Nothing in it is a credential, a prompt or a model's reasoning: only the questions, the answers, which
model and graph version produced them, the replay view a question was asked in and visible boundary/error rows.

The standard library only (Python 3.8): the control service runs on the WSL host.
"""

import hashlib
import json
import math
import os
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

SCHEMA_VERSION = 1
MAX_MESSAGE_BYTES = 64 * 1024
MAX_FIELD_BYTES = 2048          # one field of the replay context
MAX_ROW_CONTEXT_BYTES = 8192    # a stored context, boundary rows included
ROLES = ("user", "assistant", "boundary", "error")
CONTEXT_KEYS = ("replay_time", "camera", "selected_entity")   # the replay view a question may carry

MESSAGES, META, QUARANTINE, TRASH = "messages.jsonl", "meta.json", "quarantine.jsonl", ".trash-"
DEFAULT_CONTEXT_CHARS = 48000   # the conversation a question is sent with, in characters ...
DEFAULT_CONTEXT_BYTES = 60000   # ... and in UTF-8 bytes (a provider call accepts 64 KiB of conversation and replay context)
LINE_CHARS = 400                # one line of the summary of older exchanges
SUMMARY_HEADER = "Earlier conversation summary:"


class HistoryError(ValueError):
	"""History that cannot be stored or read; the message is safe to show (it names no host path)."""


def _is_number(value: Any) -> bool:
	return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _text(value: Any, limit: int = 256) -> bool:
	return isinstance(value, str) and 0 < len(value) <= limit


def history_key(canonical_path: Path) -> str:
	"""SHA-256 of the canonical UTF-8 path: every spelling of one result (links, `..`, a trailing slash) shares its history."""
	return hashlib.sha256(os.fsencode(str(Path(canonical_path).resolve()))).hexdigest()


def _byte_size(text: str) -> int:
	try:
		return len(text.encode("utf-8"))
	except UnicodeEncodeError:
		raise HistoryError("the text is not valid Unicode")


def clip_message(text: str, limit: int = MAX_MESSAGE_BYTES) -> str:
	"""The text cut to at most `limit` UTF-8 bytes (on a character boundary); the one place a long answer may be shortened."""
	data = text.encode("utf-8", "replace")
	return text if len(data) <= limit else data[:limit].decode("utf-8", "ignore")


# ---------------------------------------------------------------------------------------------- the replay view
def _plain(value: Any, depth: int = 0) -> bool:
	if value is None or isinstance(value, (bool, str)):
		return True
	if isinstance(value, (int, float)):
		return math.isfinite(value)
	if depth >= 3:
		return False
	if isinstance(value, (list, tuple)):
		return len(value) <= 16 and all(_plain(item, depth + 1) for item in value)
	if isinstance(value, Mapping):
		return len(value) <= 16 and all(isinstance(key, str) and _plain(item, depth + 1) for key, item in value.items())
	return False


def _plain_mapping(value: Any) -> bool:
	return isinstance(value, Mapping) and len(value) <= 16 and all(isinstance(key, str) and _plain(item) for key, item in value.items())


def sanitize_context(context: Any) -> Optional[Dict[str, Any]]:
	"""The replay view that may accompany a question: only the replay fields, each a small plain JSON value; None when empty."""
	if not isinstance(context, Mapping):
		return None
	kept: Dict[str, Any] = {}
	for key in CONTEXT_KEYS:
		if key in context and context[key] is not None and _plain(context[key]):
			encoded = json.dumps(context[key], ensure_ascii=False, separators=(",", ":"))
			if len(encoded.encode("utf-8", "replace")) <= MAX_FIELD_BYTES:
				kept[key] = json.loads(encoded)
	return kept or None


# ---------------------------------------------------------------------------------------------- messages
@dataclass(frozen=True)
class GraphRef:
	"""Which scene graph a message belongs to: the file and its version token (a new content of one file is a new version)."""
	name: str
	version: str

	@classmethod
	def of(cls, value: Any) -> "GraphRef":
		"""A GraphRef from a GraphRef, a {"name", "version"|"token"} mapping or an object with `name` and `token` (ArtifactVersion)."""
		if isinstance(value, cls):
			return value
		if isinstance(value, Mapping):
			name, version = value.get("name"), value.get("version", value.get("token"))
		else:
			name, version = getattr(value, "name", None), getattr(value, "token", None)
		if not _text(name) or not _text(version):
			raise HistoryError("a graph needs a file name and a version")
		return cls(name, version)


@dataclass(frozen=True)
class ChatMessage:
	"""One row of a conversation.

	role is user, assistant, boundary (the scene graph changed; `context` then holds {"from": {"name", "version"}}) or error.
	For a user message `context` is the replay view it was asked in.
	"""
	message_id: str
	role: str
	content: str
	created_at: float
	model: Optional[str] = None
	graph_name: Optional[str] = None
	graph_version: Optional[str] = None
	context: Optional[Dict[str, Any]] = None

	def to_json(self) -> Dict[str, Any]:
		return {
			"message_id": self.message_id, "role": self.role, "content": self.content, "created_at": self.created_at,
			"model": self.model, "graph_name": self.graph_name, "graph_version": self.graph_version, "context": self.context,
		}

	@classmethod
	def from_json(cls, raw: Any) -> "ChatMessage":
		if not isinstance(raw, Mapping):
			raise HistoryError("a history row is not an object")
		if not _text(raw.get("message_id"), 64) or raw.get("role") not in ROLES:
			raise HistoryError("a history row has no valid id and role")
		content = raw.get("content")
		if not isinstance(content, str) or _byte_size(content) > MAX_MESSAGE_BYTES:
			raise HistoryError("a history row has no valid content")
		if not _is_number(raw.get("created_at")):
			raise HistoryError("a history row has no valid time")
		for key in ("model", "graph_name", "graph_version"):
			if raw.get(key) is not None and not _text(raw[key]):
				raise HistoryError("a history row has a malformed " + key)
		context = raw.get("context")
		if context is not None and (not _plain_mapping(context) or len(json.dumps(context).encode("utf-8", "replace")) > MAX_ROW_CONTEXT_BYTES):
			raise HistoryError("a history row has a malformed context")
		return cls(raw["message_id"], raw["role"], content, float(raw["created_at"]), raw.get("model"), raw.get("graph_name"),
			raw.get("graph_version"), dict(context) if context is not None else None)


# ---------------------------------------------------------------------------------------------- files
_LOCKS: Dict[str, "threading.RLock"] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(directory: str) -> "threading.RLock":
	with _LOCKS_GUARD:
		return _LOCKS.setdefault(directory, threading.RLock())


def _fsync_directory(path: Path) -> None:
	try:
		descriptor = os.open(str(path), os.O_RDONLY)
	except OSError:
		return
	try:
		os.fsync(descriptor)
	except OSError:
		pass
	finally:
		os.close(descriptor)


def _write_private(path: Path, data: bytes) -> None:
	"""Replace `path` by `data` all at once: a temporary sibling, flushed to disk, then renamed over it."""
	temporary = path.with_name(".%s.tmp-%d-%s" % (path.name, os.getpid(), secrets.token_hex(4)))
	descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
	try:
		with os.fdopen(descriptor, "wb") as stream:
			stream.write(data)
			stream.flush()
			os.fsync(stream.fileno())
		os.replace(str(temporary), str(path))
	except BaseException:
		try:
			os.unlink(str(temporary))
		except OSError:
			pass
		raise
	_fsync_directory(path.parent)


def _remove_trash(root: Path, path: Path) -> None:
	"""Delete one trash entry: only a direct child of the history root named like trash, and a link is unlinked, never followed."""
	if os.path.dirname(os.path.abspath(str(path))) != os.path.abspath(str(root)) or not path.name.startswith(TRASH):
		raise HistoryError("refusing to delete a path outside the history root")
	if os.path.islink(str(path)) or not os.path.isdir(str(path)):
		os.unlink(str(path))
	else:
		shutil.rmtree(str(path))


def remove_stale_trash(root: Path) -> None:
	"""Remove what an interrupted clear left in the history root (cleared conversations are private)."""
	root = Path(root)
	try:
		names = sorted(os.listdir(str(root)))
	except OSError:
		return
	for name in names:
		if name.startswith(TRASH):
			try:
				_remove_trash(root, root / name)
			except OSError:
				pass


# ---------------------------------------------------------------------------------------------- summary of older turns
def _one_line(text: str) -> str:
	return " ".join(text.split())


def _shorten(text: str, limit: int) -> str:
	return text if len(text) <= limit else text[:max(0, limit - 1)] + "…"


def _summary_line(question: Optional[ChatMessage], answer: Optional[ChatMessage]) -> str:
	"""One exchange as a single line of at most LINE_CHARS characters, with the graph its answer came from."""
	source = answer or question
	label = ""
	if source.graph_name and source.graph_version:
		label = "[%s %s] " % (_shorten(source.graph_name, 60), _shorten(source.graph_version, 40))
	head = "- " + label
	if question is not None and answer is not None:
		asked = _shorten(_one_line(question.content), 120)
		prefix = "%sQ: %s A: " % (head, asked)
		line = prefix + _shorten(_one_line(answer.content), max(LINE_CHARS - len(prefix), 1))
	elif question is not None:
		line = "%sQ: %s (no answer)" % (head, _shorten(_one_line(question.content), 120))
	else:
		line = "%sA: %s" % (head, _shorten(_one_line(answer.content), max(LINE_CHARS - len(head) - 3, 1)))
	return _shorten(line, LINE_CHARS)


def _cost(text: str) -> Tuple[int, int]:
	return len(text), len(text.encode("utf-8", "replace"))


# ---------------------------------------------------------------------------------------------- the store
class ConversationStore:
	"""The conversation of one result."""

	def __init__(self, root: Path, canonical_path: Path):
		self.root = Path(root)
		self.canonical_path = Path(canonical_path).resolve()
		self.key = history_key(self.canonical_path)
		self.directory = self.root / self.key
		self._lock = _lock_for(str(self.directory))

	# -------------------------------------------------------------- reading
	def messages(self) -> List[ChatMessage]:
		"""Every stored row, oldest first. Rows that cannot be read are moved to quarantine.jsonl and the rest stay."""
		with self._lock:
			return self._load()

	def last_graph(self) -> Optional[GraphRef]:
		"""The graph of the newest row that names one, whatever its role."""
		with self._lock:
			return self._last_graph(self._load())

	@staticmethod
	def _last_graph(messages: List[ChatMessage]) -> Optional[GraphRef]:
		for message in reversed(messages):
			if message.graph_name and message.graph_version:
				return GraphRef(message.graph_name, message.graph_version)
		return None

	def _check_directory(self) -> None:
		if os.path.islink(str(self.directory)):
			raise HistoryError("the history directory is a link")

	def _read_meta(self) -> Optional[Dict[str, Any]]:
		"""meta.json, None when it is missing or unreadable (it is then rewritten); HistoryError for a newer schema."""
		try:
			meta = json.loads((self.directory / META).read_text(encoding="utf-8"))
		except (OSError, ValueError):
			return None
		if not isinstance(meta, dict) or isinstance(meta.get("schema_version"), bool) or not isinstance(meta.get("schema_version"), int):
			return None
		if meta["schema_version"] > SCHEMA_VERSION:
			raise HistoryError("this history was written by a newer version of the replay service")
		return meta

	def _load(self) -> List[ChatMessage]:
		self._check_directory()
		if not os.path.isdir(str(self.directory)):
			return []
		self._read_meta()
		try:
			data = (self.directory / MESSAGES).read_bytes()
		except FileNotFoundError:
			return []
		messages: List[ChatMessage] = []
		good: List[bytes] = []
		bad: List[bytes] = []
		for line in data.split(b"\n"):
			if not line.strip():
				continue
			try:
				messages.append(ChatMessage.from_json(json.loads(line.decode("utf-8"))))
				good.append(line)
			except (UnicodeDecodeError, ValueError, RecursionError):
				bad.append(line)
		if bad or (data and not data.endswith(b"\n")):
			self._repair(good, bad)
		return messages

	def _repair(self, good: List[bytes], bad: List[bytes]) -> None:
		"""Keep the unreadable lines apart (first, so a crash loses none) and leave only whole lines in messages.jsonl."""
		if bad:
			rows = b"".join(
				json.dumps({"quarantined_at": time.time(), "line": line.decode("utf-8", "replace")[:MAX_MESSAGE_BYTES]},
					ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
				for line in bad
			)
			self._append_bytes(self.directory / QUARANTINE, rows)
		_write_private(self.directory / MESSAGES, b"".join(line + b"\n" for line in good))

	# -------------------------------------------------------------- writing
	def _ensure(self) -> None:
		self._check_directory()
		created = not os.path.isdir(str(self.directory))
		os.makedirs(str(self.directory), mode=0o700, exist_ok=True)
		if created:
			_fsync_directory(self.directory.parent)
		if self._read_meta() is None:
			meta = {"schema_version": SCHEMA_VERSION, "canonical_path": str(self.canonical_path), "created_at": time.time()}
			_write_private(self.directory / META, self._encode(meta))

	@staticmethod
	def _encode(value: Mapping[str, Any]) -> bytes:
		return json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")

	@staticmethod
	def _append_bytes(path: Path, data: bytes) -> None:
		new = not path.exists()
		descriptor = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
		with os.fdopen(descriptor, "ab") as stream:
			stream.write(data)
			stream.flush()
			os.fsync(stream.fileno())
		if new:
			_fsync_directory(path.parent)

	def _append(self, role: str, content: str, model: Optional[str], graph: Optional[GraphRef], context: Optional[Dict[str, Any]]) -> ChatMessage:
		if not isinstance(content, str) or (role in ("user", "assistant") and not content.strip()):
			raise HistoryError("a message needs text")
		if _byte_size(content) > MAX_MESSAGE_BYTES:
			raise HistoryError("a message is limited to %d bytes" % MAX_MESSAGE_BYTES)
		if model is not None and not _text(model):
			raise HistoryError("the model name is malformed")
		message = ChatMessage(secrets.token_hex(8), role, content, time.time(), model,
			graph.name if graph else None, graph.version if graph else None, context)
		line = json.dumps(message.to_json(), ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
		self._ensure()
		self._append_bytes(self.directory / MESSAGES, line)
		return message

	def append_user(self, content: str, *, model: str, graph: Any, context: Optional[Mapping[str, Any]] = None) -> ChatMessage:
		"""A question, with the model it is sent to, the graph version it is asked against and the replay view it carries."""
		if not _text(model):
			raise HistoryError("a question names the model it is sent to")
		reference = GraphRef.of(graph)
		with self._lock:
			return self._append("user", content, model, reference, sanitize_context(context))

	def append_assistant(self, content: str, *, model: str, graph: Any) -> ChatMessage:
		"""An answer with the model that wrote it and the graph version it was computed against."""
		if not _text(model):
			raise HistoryError("an answer names the model that wrote it")
		reference = GraphRef.of(graph)
		with self._lock:
			return self._append("assistant", content, model, reference, None)

	def append_error(self, content: str, *, model: Optional[str] = None, graph: Any = None) -> ChatMessage:
		"""A visible failure row. It is kept in the history but never sent to a model."""
		reference = GraphRef.of(graph) if graph is not None else None
		with self._lock:
			return self._append("error", content, model, reference, None)

	def append_boundary(self, graph: Any) -> Optional[ChatMessage]:
		"""Record that the conversation moved to another graph version.

		Nothing is written for the first message of a conversation or when the version is the one last used, so consecutive
		questions against one version stay free of boundaries.
		"""
		reference = GraphRef.of(graph)
		with self._lock:
			previous = self._last_graph(self._load())
			if previous is None or previous == reference:
				return None
			text = "The scene graph changed from %s to %s." % (previous.name, reference.name)
			return self._append("boundary", text, None, reference, {"from": {"name": previous.name, "version": previous.version}})

	# -------------------------------------------------------------- what a model is sent
	def context_messages(self, max_chars: int = DEFAULT_CONTEXT_CHARS, max_bytes: int = DEFAULT_CONTEXT_BYTES) -> List[Dict[str, str]]:
		"""The earlier conversation of a new question as provider-neutral {"role", "content"} turns.

		Boundary and error rows are never sent. The newest whole messages that fit two thirds of the budget are kept; older
		exchanges become one extractive line each (question, answer, the graph the answer came from; at most LINE_CHARS
		characters) under "Earlier conversation summary:", newest first until the rest of the budget is used, with the number of
		exchanges that did not fit. The result stays within `max_chars` characters and `max_bytes` UTF-8 bytes. No model is
		involved: the same history always gives the same turns.
		"""
		with self._lock:
			turns = [message for message in self._load() if message.role in ("user", "assistant")]
			window_chars, window_bytes = max(1, max_chars * 2 // 3), max(1, max_bytes * 2 // 3)
			window: List[ChatMessage] = []
			used_chars = used_bytes = 0
			for message in reversed(turns):
				chars, size = _cost(message.content)
				if window and (used_chars + chars > window_chars or used_bytes + size > window_bytes):
					break
				if not window and (chars > window_chars or size > window_bytes):
					message = ChatMessage(message.message_id, message.role, clip_message(message.content[:window_chars], window_bytes),
						message.created_at, message.model, message.graph_name, message.graph_version, message.context)
					chars, size = _cost(message.content)
				window.append(message)
				used_chars += chars
				used_bytes += size
			window.reverse()
			older = turns[:len(turns) - len(window)]
			while len(window) > 1 and window[0].role == "assistant":   # an answer whose question did not fit
				older.append(window.pop(0))
				used_chars -= _cost(older[-1].content)[0]
				used_bytes -= _cost(older[-1].content)[1]
			result = [{"role": message.role, "content": message.content} for message in window]
			summary = self._summary(older, max_chars - used_chars, max_bytes - used_bytes)
			if summary:
				result.insert(0, {"role": "user", "content": summary})
				self._remember_summary(older, summary)
			return result

	@staticmethod
	def _summary(older: List[ChatMessage], chars: int, size: int) -> str:
		"""The summary of older turns within the given budgets, or "" when there are none or nothing fits."""
		if not older:
			return ""
		exchanges: List[Tuple[Optional[ChatMessage], Optional[ChatMessage]]] = []
		index = 0
		while index < len(older):
			if older[index].role == "user" and index + 1 < len(older) and older[index + 1].role == "assistant":
				exchanges.append((older[index], older[index + 1]))
				index += 2
			elif older[index].role == "user":
				exchanges.append((older[index], None))
				index += 1
			else:
				exchanges.append((None, older[index]))
				index += 1
		reserved = _cost(SUMMARY_HEADER + "\n(%d earlier exchanges omitted)\n" % len(exchanges))
		used_chars, used_bytes = reserved
		kept: List[str] = []
		for question, answer in reversed(exchanges):
			line = _summary_line(question, answer)
			line_chars, line_bytes = _cost(line + "\n")
			if used_chars + line_chars > chars or used_bytes + line_bytes > size:
				break
			kept.append(line)
			used_chars += line_chars
			used_bytes += line_bytes
		if not kept:
			return ""
		kept.reverse()
		omitted = len(exchanges) - len(kept)
		lines = [SUMMARY_HEADER]
		if omitted:
			lines.append("(%d earlier exchanges omitted)" % omitted)
		return "\n".join(lines + kept)

	def _remember_summary(self, older: List[ChatMessage], summary: str) -> None:
		"""Keep the summary in meta.json (the stored messages stay whole). A failure here never fails a question."""
		try:
			meta = self._read_meta()
			record = {"through": older[-1].message_id, "messages": len(older), "text": summary}
			if meta is None or meta.get("summary") == record:
				return
			meta["summary"] = record
			_write_private(self.directory / META, self._encode(meta))
		except (OSError, HistoryError):
			pass

	# -------------------------------------------------------------- clearing
	def clear(self) -> None:
		"""Forget this result's conversation: the directory is renamed to a trash sibling in one step, then that sibling is removed.

		Only this result's own directory (a direct child of the history root) is touched, and a link is never followed. The next
		message starts a new conversation.
		"""
		with self._lock:
			if not os.path.lexists(str(self.directory)):
				return
			trash = self.root / (TRASH + self.key[:16] + "-" + secrets.token_hex(4))
			os.rename(str(self.directory), str(trash))
			_fsync_directory(self.root)
			_remove_trash(self.root, trash)
