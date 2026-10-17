"""The scene graph each open result currently shows, chosen from its graph files and watched for changes.

A result directory holds up to four scene graphs, from most to least processed (GRAPH_PRIORITY): the summarized
clustering, the clustering, the embedded graph and the raw pipeline graph. The active graph is the most processed one
that is valid. A file is valid when it is a complete UTF-8 JSON object with a list of scene graph nodes; a missing,
partial or broken file never becomes active, and an active graph whose file is rewritten into something invalid keeps
its last valid version (a running job may be writing it).

`ArtifactMonitor` polls the open results (every two seconds by default; file notifications are unreliable across
WSL, /mnt drives and Docker writes). A poll only stats the four files; a file is read again only when its size,
modification time, change time or inode changed, and a new version needs a new content digest. Files below the active
one are not parsed until they could become active. Only a compact summary of the active graph (`normalize_graph`: the
objects, places and rooms the viewer and later the Agent context need, without meshes, features, embeddings or model
reasoning) is kept in memory.

Every change is reported through one callback, `callback(run_id, event_type, payload)`:

* "graph" when the active graph changes (reason initial, none, activated, promoted, demoted, updated or removed);
* "artifact-error" once per new invalid version of a file that could have become active.

Standard library only, Python 3.8: this runs in the WSL control service, which never imports spark_dsg.
"""

import hashlib
import json
import math
import os
import stat as stat_module
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from scripts.replay_web.manifest import GRAPH_CANDIDATES

GRAPH_PRIORITY: Tuple[str, ...] = GRAPH_CANDIDATES   # most processed first: the order the active graph is chosen in
DOCUMENT_SCHEMA = 1
MAX_GRAPH_BYTES = 512 * 1024 * 1024
DEFAULT_INTERVAL = 2.0
FRESH_SECONDS = 1.0   # a file read this soon after its last change is read again at the next poll (see _parse)
MAX_TEXT = 2000
LAYER_NAMES = {
	(2, 0): "OBJECTS", (2, 2): "BACKGROUND_OBJECTS", (2, ord("a")): "AGENTS", (3, 0): "PLACES", (3, 1): "MESH_PLACES",
	(3, 2): "TRAVERSABILITY", (4, 0): "ROOMS", (5, 0): "BUILDINGS",
}
_SYMBOL_MASK = (1 << 56) - 1

Callback = Callable[[str, str, Mapping[str, Any]], None]


class ArtifactError(ValueError):
	"""A graph file that cannot be used; the message names the file but never its host path."""


class ArtifactChanging(ArtifactError):
	"""The file changed while it was read: look again at the next poll."""


@dataclass(frozen=True)
class ArtifactVersion:
	name: str
	path: Path
	size: int
	mtime_ns: int
	digest: str

	@property
	def rank(self) -> int:
		return GRAPH_PRIORITY.index(self.name)

	@property
	def token(self) -> str:
		"""The version token the browser compares and caches by: the file's rank and its content digest."""
		return "%d-%s" % (self.rank, self.digest[:20])

	def to_json(self) -> Dict[str, Any]:
		return {
			"name": self.name, "rank": self.rank, "token": self.token, "digest": self.digest, "size": self.size,
			"modified": round(self.mtime_ns / 1e9, 3),
		}


@dataclass(frozen=True)
class ActiveGraph:
	name: str
	version: ArtifactVersion
	document: Mapping[str, Any] = field(repr=False)


def _reason(error: BaseException) -> str:
	"""Why an operation failed, without the host paths that str(error) may carry (these messages reach the browser)."""
	if isinstance(error, OSError):
		return error.strerror or type(error).__name__
	if isinstance(error, RuntimeError):
		return "symbolic link loop"
	return type(error).__name__


def _fingerprint(value: Any) -> Tuple[int, int, int, int, int]:
	return (value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_ino, value.st_dev)


def _read_stable(path: Path, max_bytes: int) -> Tuple[bytes, Any]:
	"""The whole file and the status it had while it was read; ArtifactChanging when it changed meanwhile."""
	name = path.name
	try:
		with path.open("rb") as stream:
			before = os.fstat(stream.fileno())
			if not stat_module.S_ISREG(before.st_mode):
				raise ArtifactError(f"{name} is not a regular file")
			if before.st_size > max_bytes:
				raise ArtifactError(f"{name} is {before.st_size} bytes, over the {max_bytes} byte limit for a scene graph")
			raw = stream.read(max_bytes + 1)
		after = os.stat(path)
	except FileNotFoundError as error:
		raise ArtifactChanging(f"{name} disappeared while it was read") from error
	except OSError as error:
		raise ArtifactError(f"{name} cannot be read ({_reason(error)})") from error
	if len(raw) > max_bytes:
		raise ArtifactError(f"{name} grew over the {max_bytes} byte limit for a scene graph")
	if _fingerprint(before) != _fingerprint(after) or len(raw) != before.st_size:
		raise ArtifactChanging(f"{name} changed while it was read")
	return raw, before


def _is_int(value: Any) -> bool:
	return isinstance(value, int) and not isinstance(value, bool)


def _parse_graph(name: str, raw: bytes) -> Dict[str, Any]:
	try:
		text = raw.decode("utf-8")
	except UnicodeDecodeError as error:
		raise ArtifactError(f"{name} is not UTF-8 text (byte {error.start})") from error
	try:
		value = json.loads(text)
	except ValueError as error:
		raise ArtifactError(f"{name} is not complete JSON ({error})") from error
	if not isinstance(value, dict) or not isinstance(value.get("nodes"), list):
		raise ArtifactError(f"{name} is not a scene graph JSON object (no node list)")
	for index, item in enumerate(value["nodes"]):
		if not isinstance(item, dict) or not _is_int(item.get("id")) or not _is_int(item.get("layer")):
			raise ArtifactError(f"{name}: node {index} has no integer id and layer")
		if "attributes" in item and not isinstance(item["attributes"], dict):
			raise ArtifactError(f"{name}: node {index} has malformed attributes")
	edges = value.get("edges", [])
	if not isinstance(edges, list):
		raise ArtifactError(f"{name} has a malformed edge list")
	for index, item in enumerate(edges):
		if not isinstance(item, dict) or not _is_int(item.get("source")) or not _is_int(item.get("target")):
			raise ArtifactError(f"{name}: edge {index} has no integer source and target")
	return value


def _read_graph_file(path: Path, root: Optional[Path], max_bytes: int) -> Tuple[bytes, Any]:
	if root is not None:
		try:
			resolved = path.resolve(strict=True)
		except FileNotFoundError as error:
			raise ArtifactChanging(f"{path.name} disappeared") from error
		except (OSError, RuntimeError) as error:
			raise ArtifactError(f"{path.name} cannot be resolved ({_reason(error)})") from error
		try:
			resolved.relative_to(Path(root))
		except ValueError as error:
			raise ArtifactError(f"{path.name} points outside the result directory") from error
	return _read_stable(path, max_bytes)


def _version(path: Path, raw: bytes, status: Any) -> ArtifactVersion:
	return ArtifactVersion(path.name, path, status.st_size, status.st_mtime_ns, hashlib.sha256(raw).hexdigest())


def load_valid_graph(path: Path, *, root: Optional[Path] = None, max_bytes: int = MAX_GRAPH_BYTES) -> Tuple[Dict[str, Any], ArtifactVersion]:
	"""Parse and validate one graph file. With `root`, the file (after symlinks) must lie inside that directory."""
	path = Path(path)
	raw, status = _read_graph_file(path, root, max_bytes)
	return _parse_graph(path.name, raw), _version(path, raw, status)


def select_active_graph(result_dir: Path, *, max_bytes: int = MAX_GRAPH_BYTES) -> Optional[Tuple[Dict[str, Any], ArtifactVersion]]:
	"""The most processed valid graph of a result directory, or None."""
	root = Path(result_dir).resolve()
	for name in GRAPH_PRIORITY:
		path = root / name
		if not os.path.lexists(str(path)):
			continue
		try:
			return load_valid_graph(path, root=root, max_bytes=max_bytes)
		except ArtifactError:
			continue
	return None


# ---------------------------------------------------------------------------------------------- compact document
def _symbol(node: int) -> str:
	char = node >> 56
	if 65 <= char <= 90 or 97 <= char <= 122:
		return "%s%d" % (chr(char), node & _SYMBOL_MASK)
	return str(node)


def _index(node: int) -> int:
	return node & _SYMBOL_MASK


def _vector(value: Any) -> Optional[List[float]]:
	if isinstance(value, (list, tuple)) and len(value) == 3 and all(
		isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) for item in value
	):
		return [round(float(item), 3) for item in value]
	return None


def _text(value: Any) -> str:
	if not isinstance(value, str):
		return ""
	value = value.strip()
	return value if len(value) <= MAX_TEXT else value[:MAX_TEXT - 1] + "…"


def _count(value: Any) -> Optional[int]:
	return value if _is_int(value) and value >= 0 else None


def _model_name(provenance: Any, key: str) -> Optional[str]:
	entry = provenance.get(key) if isinstance(provenance, dict) else None
	name = entry.get("model_name") if isinstance(entry, dict) else None
	return _text(name) or None


def normalize_graph(value: Mapping[str, Any]) -> Dict[str, Any]:
	"""The part of a validated scene graph that the workspace keeps: objects, places and rooms with their links."""
	layer_of: Dict[int, Tuple[int, int]] = {}
	counts: Dict[str, int] = {}
	for item in value["nodes"]:
		key = (item["layer"], item.get("partition", 0) if _is_int(item.get("partition", 0)) else 0)
		layer_of[item["id"]] = key
		name = LAYER_NAMES.get(key, "LAYER_%d_%d" % key)
		counts[name] = counts.get(name, 0) + 1
	parent: Dict[int, int] = {}
	for edge in value.get("edges", []):
		source, target = edge["source"], edge["target"]
		if source in layer_of and target in layer_of and layer_of[source][0] > layer_of[target][0]:
			parent.setdefault(target, source)

	def room_of(node: int) -> Optional[int]:
		place = parent.get(node)
		if place is not None and layer_of[place][0] == 3:
			node = place
		room = parent.get(node)
		return room if room is not None and layer_of[room][0] == 4 else None

	objects, places, rooms = [], [], []
	room_places: Dict[int, int] = {}
	room_objects: Dict[int, int] = {}
	embedded = 0
	for item in value["nodes"]:
		attributes = item.get("attributes") or {}
		metadata = attributes.get("metadata") if isinstance(attributes.get("metadata"), dict) else {}
		key, node = layer_of[item["id"]], item["id"]
		if key == (2, 0):
			room = room_of(node)
			if room is not None:
				room_objects[room] = room_objects.get(room, 0) + 1
			if metadata.get("sentence_embedding_feature"):
				embedded += 1
			place = parent.get(node)
			objects.append({
				"id": _symbol(node),
				"label": _count(attributes.get("semantic_label")),
				"name": _text(attributes.get("name")),
				"description": _text(metadata.get("description")),
				"position": _vector(attributes.get("position")),
				"place": _symbol(place) if place is not None and layer_of[place][0] == 3 else None,
				"room": _symbol(room) if room is not None else None,
				"_order": _index(node),
			})
		elif key[0] == 3:
			room = parent.get(node)
			if room is not None and layer_of[room][0] == 4:
				room_places[room] = room_places.get(room, 0) + 1
			else:
				room = None
			if key == (3, 0):
				places.append({"id": _symbol(node), "room": _symbol(room) if room is not None else None, "_order": _index(node)})
		elif key[0] == 4:
			rooms.append((node, attributes, metadata))
	room_items = []
	for node, attributes, metadata in rooms:
		description = _text(metadata.get("region_description")) or _text(metadata.get("description"))
		room_items.append({
			"id": _symbol(node),
			"name": _text(attributes.get("name")),
			"label": _text(metadata.get("region_label")) or None,
			"description": description,
			"position": _vector(attributes.get("position")),
			"num_objects": _count(metadata.get("num_objects")),
			"places": room_places.get(node, 0),
			"linked_objects": room_objects.get(node, 0),   # through edges only: a graph may assign objects by position instead
			"_order": _index(node),
		})
	for items in (objects, places, room_items):
		items.sort(key=lambda entry: entry["_order"])
		for entry in items:
			del entry["_order"]
	header = value.get("SPARK_DSG_header") if isinstance(value.get("SPARK_DSG_header"), dict) else {}
	release = header.get("version") if isinstance(header.get("version"), dict) else {}
	metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
	provenance = metadata.get("embedding_provenance")
	return {
		"schema": DOCUMENT_SCHEMA,
		"spark_dsg": ".".join(str(release[key]) for key in ("major", "minor", "patch") if _is_int(release.get(key))) or None,
		"counts": counts,
		"objects": objects,
		"places": places,
		"rooms": room_items,
		"summaries": sum(1 for room in room_items if room["label"]),
		"embeddings": {"sentence": _model_name(provenance, "sentence"), "clip": _model_name(provenance, "clip"), "objects": embedded},
	}


# ---------------------------------------------------------------------------------------------- monitor
@dataclass
class _Candidate:
	"""What the monitor knows about one graph file: present (not parsed), valid, invalid, or changing (look again)."""

	state: str
	fingerprint: Optional[Tuple[int, int, int, int, int]] = None
	version: Optional[ArtifactVersion] = None
	document: Optional[Mapping[str, Any]] = field(default=None, repr=False)
	error: Optional[str] = None
	recheck: bool = False   # read just after a change: confirm its digest at the next poll

	def to_json(self) -> Dict[str, Any]:
		entry: Dict[str, Any] = {"state": self.state}
		if self.version is not None:
			entry["token"] = self.version.token
		if self.error:
			entry["error"] = self.error
		return entry


@dataclass
class _RunState:
	canonical_path: Path
	scanned: bool = False
	active: Optional[ActiveGraph] = None
	candidates: Dict[str, _Candidate] = field(default_factory=dict)

	def artifacts(self) -> Dict[str, Dict[str, Any]]:
		return {name: self.candidates[name].to_json() if name in self.candidates else {"state": "absent"} for name in GRAPH_PRIORITY}


def _active_key(active: Optional[ActiveGraph]) -> Optional[Tuple[str, str]]:
	return (active.name, active.version.digest) if active is not None else None


class ArtifactMonitor:
	def __init__(self, registry: Any, callback: Optional[Callback], interval: float = DEFAULT_INTERVAL, *,
			settle: Optional[float] = None, max_bytes: int = MAX_GRAPH_BYTES):
		self.registry = registry
		self.callback = callback
		self.interval = float(interval)
		self.settle = self.interval if settle is None else float(settle)   # a fresh broken file may still be written
		self.max_bytes = max_bytes
		self._states: Dict[str, _RunState] = {}
		self._lock = threading.Lock()   # guards _states; never held during file reads or callbacks
		self._scan_lock = threading.Lock()   # one scan at a time: at most one large graph is parsed at once
		self._wake = threading.Event()
		self._stop = threading.Event()
		self._thread: Optional[threading.Thread] = None

	# ------------------------------------------------------------------ lifecycle
	def start(self) -> None:
		if self._thread is not None:
			return
		self._stop.clear()
		self._wake.set()
		if hasattr(self.registry, "add_listener"):
			self.registry.add_listener(self._registry_changed)
		self._thread = threading.Thread(target=self._loop, name="replay-artifact-monitor", daemon=True)
		self._thread.start()

	def stop(self, timeout: float = 30.0) -> None:
		thread, self._thread = self._thread, None
		if thread is None:
			return
		if hasattr(self.registry, "remove_listener"):
			self.registry.remove_listener(self._registry_changed)
		self._stop.set()
		self._wake.set()
		thread.join(timeout)

	def request_scan(self) -> None:
		"""Scan soon (a result was opened or a job finished) instead of at the next interval."""
		self._wake.set()

	def _registry_changed(self, event: str, run: Any) -> None:
		if event == "close":
			with self._lock:
				self._states.pop(run.run_id, None)
		self.request_scan()

	def _loop(self) -> None:
		while True:
			self._wake.wait(self.interval)
			self._wake.clear()
			if self._stop.is_set():
				return
			try:
				self.scan_once(self._stop.is_set)
			except Exception:   # pragma: no cover - scan_once already isolates runs
				traceback.print_exc()

	# ------------------------------------------------------------------ queries
	def active(self, run_id: str) -> Optional[ActiveGraph]:
		with self._lock:
			state = self._states.get(run_id)
			return state.active if state is not None else None

	def view(self, run_id: str) -> Dict[str, Any]:
		"""Everything known about a run's graphs at one instant: scan state, active version and document, every file's state."""
		with self._lock:
			state = self._states.get(run_id)
			if state is None:
				return {"scanned": False, "version": None, "document": None, "artifacts": {name: {"state": "unknown"} for name in GRAPH_PRIORITY}}
			active = state.active
			return {
				"scanned": state.scanned,
				"version": active.version.to_json() if active is not None else None,
				"document": active.document if active is not None else None,
				"artifacts": state.artifacts(),
			}

	def status(self, run_id: str) -> Dict[str, Any]:
		"""`view` without the document: what the `state` event carries."""
		view = self.view(run_id)
		return {"scanned": view["scanned"], "graph": view["version"], "artifacts": view["artifacts"]}

	# ------------------------------------------------------------------ scanning
	def scan_once(self, should_stop: Optional[Callable[[], bool]] = None) -> None:
		"""Check every open result once; a failure in one result is logged and does not stop the others."""
		with self._scan_lock:
			runs = list(self.registry.runs())
			open_ids = {run.run_id for run in runs}
			with self._lock:
				for run_id in [run_id for run_id in self._states if run_id not in open_ids]:
					del self._states[run_id]
			for run in runs:
				if should_stop is not None and should_stop():
					return
				try:
					events = self._scan_run(run)
				except Exception:
					sys.stderr.write(f"[replay-web] scene graph scan of run {run.run_id} failed:\n{traceback.format_exc()}")
					continue
				for event_type, payload in events:
					self._emit(run.run_id, event_type, payload)

	def _emit(self, run_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
		if self.callback is None:
			return
		try:
			self.callback(run_id, event_type, payload)
		except Exception:
			sys.stderr.write(f"[replay-web] {event_type} listener failed for run {run_id}:\n{traceback.format_exc()}")

	def _scan_run(self, run: Any) -> List[Tuple[str, Dict[str, Any]]]:
		root = Path(run.canonical_path)
		with self._lock:
			state = self._states.get(run.run_id)
			if state is None or state.canonical_path != root:
				state = _RunState(canonical_path=root)
			previous = dict(state.candidates)
			old_active, scanned = state.active, state.scanned
		candidates: Dict[str, _Candidate] = {}
		errors: List[Tuple[str, str]] = []
		settled = False   # a graph above is valid (or kept): files below are only stat'ed, not parsed
		for name in GRAPH_PRIORITY:
			known = previous.get(name)
			try:
				status = os.stat(root / name)
			except FileNotFoundError:
				continue
			except OSError as error:
				message = f"{name} cannot be read ({_reason(error)})"
				if known is None or known.error != message:
					errors.append((name, message))
				candidates[name] = _Candidate("invalid", error=message)
				continue
			fingerprint = _fingerprint(status)
			if known is not None and known.fingerprint == fingerprint and (settled or not known.recheck):
				candidate = known
			else:
				candidate = _Candidate("present", fingerprint=fingerprint)
			if not settled and candidate.state == "present":
				candidate = self._parse(root / name, root, candidate, known, errors)
			candidates[name] = candidate
			if candidate.state == "valid" or (candidate.state == "invalid" and old_active is not None and old_active.name == name):
				settled = True
		new_active = None
		for name in GRAPH_PRIORITY:
			candidate = candidates.get(name)
			if candidate is None:
				continue
			if candidate.state == "valid":
				new_active = ActiveGraph(name, candidate.version, candidate.document)
				break
			if candidate.state == "invalid" and old_active is not None and old_active.name == name:
				new_active = old_active   # its file is broken now: the last valid version stays active
				break
		reason = None
		if not scanned:
			reason = "initial" if new_active is not None else "none"
		elif _active_key(new_active) != _active_key(old_active):
			if new_active is None:
				reason = "removed"
			elif old_active is None:
				reason = "activated"
			elif new_active.name == old_active.name:
				reason = "updated"
			else:
				reason = "promoted" if new_active.version.rank < old_active.version.rank else "demoted"
		try:
			self.registry.get(run.run_id)
		except KeyError:
			return []   # closed while it was scanned
		with self._lock:
			state.candidates, state.active, state.scanned = candidates, new_active, True
			self._states[run.run_id] = state
			artifacts = state.artifacts()
		events: List[Tuple[str, Dict[str, Any]]] = []
		if reason is not None:
			events.append(("graph", {
				"reason": reason,
				"version": new_active.version.to_json() if new_active is not None else None,
				"previous": old_active.version.to_json() if old_active is not None else None,
				"artifacts": artifacts,
			}))
		for name, message in errors:
			events.append(("artifact-error", {
				"name": name, "error": message, "active": new_active.version.to_json() if new_active is not None else None,
			}))
		return events

	def _parse(self, path: Path, root: Path, candidate: _Candidate, known: Optional[_Candidate], errors: List[Tuple[str, str]]) -> _Candidate:
		"""Read a changed (or not yet parsed) file: valid, invalid (reported once), or changing (looked at again).

		Linux file times advance in coarse ticks, so a same-size rewrite within the tick of an earlier read would keep
		the fingerprint. A file read less than FRESH_SECONDS after its change is therefore read once more at the next
		poll; an unchanged digest then costs a read and a hash, not a parse.
		"""
		try:
			raw, status = _read_graph_file(path, root, self.max_bytes)
		except ArtifactChanging:
			return _Candidate("changing")
		except ArtifactError as error:
			return self._invalid(path.name, candidate, str(error), errors)
		version = _version(path, raw, status)
		fingerprint = _fingerprint(status)
		recheck = time.time() - status.st_mtime_ns / 1e9 < FRESH_SECONDS
		if known is not None and known.version is not None and known.document is not None and known.version.digest == version.digest:
			return _Candidate("valid", fingerprint=fingerprint, version=version, document=known.document, recheck=recheck)
		try:
			value = _parse_graph(path.name, raw)
		except ArtifactError as error:
			return self._invalid(path.name, candidate, str(error), errors)
		del raw
		document = normalize_graph(value)
		return _Candidate("valid", fingerprint=fingerprint, version=version, document=document, recheck=recheck)

	def _invalid(self, name: str, candidate: _Candidate, message: str, errors: List[Tuple[str, str]]) -> _Candidate:
		if time.time() - candidate.fingerprint[1] / 1e9 < self.settle:
			return _Candidate("changing")   # a broken file this fresh may still be being written: judge it at the next poll
		errors.append((name, message))
		return _Candidate("invalid", fingerprint=candidate.fingerprint, error=message)
