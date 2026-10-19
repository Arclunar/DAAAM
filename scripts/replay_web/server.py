"""The replay workspace service: one loopback HTTP server for the repository's single replay.html.

    python3 -m scripts.replay_web.server [--host 127.0.0.1] [--port 8765]        (or: just replay-web)

It runs on the WSL host (standard library only, Python 3.8), so any WSL-visible result directory can be opened, and it
serves only:

* the application: /replay.html and the files in scripts/replay_web/static/;
* the API under /api/: the result list, opening a result by path (which returns an opaque run ID), the members of
  an open result's bundle that its validated manifest names (timeline chunks, images, the reference mesh), the active
  scene graph of an open result, and a server-sent events stream of what changes (/api/runs/{id}/events for one result,
  /api/events?run=...&run=... for several over one connection, since a browser allows only a few per origin). Processing
  jobs (scripts/replay_web/jobs.py) run in the daaam-ws container: POST /api/runs/{id}/jobs {"stage", "replace"} starts one
  of three allowlisted stages, GET lists them with what each stage may do now, POST /api/jobs/{id}/cancel stops one; their
  progress arrives on the event streams. The chat (scripts/replay_web/chat.py): GET /api/runs/{id}/chat is the conversation
  of a result and whether its graph can answer questions, POST streams the answer to one question as server-sent events (the
  answers come from one shared Agent worker in the container), DELETE forgets the conversation.

There is no general file-read or command endpoint. The service binds 127.0.0.1 only and refuses requests whose Host
header is not this loopback address (a DNS-rebound page cannot read it); POST and DELETE also need the page's Origin
and the startup CSRF token from /api/config. Bundle members are immutable per bundle version, so they are sent with
validators and long-lived cache headers; the version is part of their URL.
"""

import argparse
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import signal
import shutil
import subprocess
import sys
import threading
import traceback
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from scripts.replay_web.agent_client import AgentClient, default_argv
from scripts.replay_web.agent_protocol import allowed_models
from scripts.replay_web.artifacts import GRAPH_PRIORITY, ArtifactMonitor
from scripts.replay_web.chat import ChatError, ChatManager, Turn
from scripts.replay_web.events import EventBroker, SubscriptionClosed, encode_sse
from scripts.replay_web.jobs import JobError, JobManager, JobRecord, JobSpec, JobStage
from scripts.replay_web.manifest import SCHEMA_VERSION, ManifestError, resolve_bundle_member
from scripts.replay_web.paths import PathError
from scripts.replay_web.runs import OpenRun, RunRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "static"
LOOPBACK_HOSTS = {"127.0.0.1": "127.0.0.1", "localhost": "127.0.0.1"}
JSON_BODY_LIMIT = 64 * 1024
STATIC_TYPES = {
	".js": "text/javascript; charset=utf-8",
	".css": "text/css; charset=utf-8",
	".svg": "image/svg+xml",
	".png": "image/png",
}
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
CONTENT_SECURITY_POLICY = (
	"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; "
	"worker-src 'self' blob:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
IMMUTABLE = "private, max-age=31536000, immutable"
_STATIC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_STREAM_RUNS = 64
SSE_RETRY_MS = 2000


@dataclass(frozen=True)
class ServerConfig:
	repo_root: Path
	output_root: Path
	state_root: Path
	distro: str
	host: str = "127.0.0.1"
	port: int = 8765
	scan_depth: int = 4
	graph_interval: float = 2.0   # seconds between looks at the scene graph files of the open results
	max_jobs: int = 1   # processing jobs that may run at the same time (the others wait their turn)
	agent_models: Optional[Tuple[str, ...]] = None   # the models a question may name (default: $REPLAY_AGENT_MODELS, else gpt-5-mini)
	agent_enabled: bool = True   # False: no question is accepted and no worker is ever started
	agent_idle_seconds: Optional[float] = None   # the Agent worker ends after this long without a question (default: its own 15 minutes)
	stage_root: Optional[Path] = None   # the repository root as the worker sees it, where graph snapshots are staged (default: repo_root; tests)
	sse_heartbeat: float = 15.0   # an idle event stream sends a comment this often, which also reveals closed connections


class HttpError(Exception):
	def __init__(self, status: int, message: str, code: Optional[str] = None):
		super().__init__(message)
		self.status = status
		self.message = message
		self.code = code


class RunAssets:
	"""What the service may send for one open result: its bundle version and the files its manifest names."""

	def __init__(self, run: OpenRun):
		self.run = run
		manifest = run.manifest
		self.version = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
		cameras = [camera["key"] for camera in manifest.get("cameras", [])]
		images: Set[str] = set()
		for entry in manifest["images"]["timeline"]:
			images.update(entry[key] for key in cameras if entry.get(key))
		images.update(entry["path"] for entry in manifest["images"]["descriptions"].values())
		self.images = images
		reference = manifest.get("reference_mesh")
		self.reference = reference["path"] if reference else None


class ReplayService:
	"""State behind the HTTP handler: one per server."""

	def __init__(self, config: ServerConfig, registry: Optional[RunRegistry] = None, jobs: Optional[JobManager] = None, agent: Any = None):
		self.config = config
		self.registry = registry or RunRegistry(config.output_root, config.distro, config.scan_depth)
		self.csrf_token = secrets.token_urlsafe(32)
		self.port = config.port
		self._assets: Dict[str, RunAssets] = {}
		self._lock = threading.Lock()
		self.broker = EventBroker(state_provider=self.state_payload)
		self.monitor = ArtifactMonitor(self.registry, self.broker.publish, interval=config.graph_interval)
		self.jobs = jobs or JobManager(state_root=config.state_root, repo_root=config.repo_root, max_concurrent=config.max_jobs)
		self.jobs.broker = self.broker
		self.jobs.path_of_run = self._path_of_run
		self.jobs.run_of_path = self._run_of_path
		self.jobs.on_finished = self._job_finished
		models = config.agent_models or allowed_models()
		self.agent = agent if agent is not None else AgentClient(
			argv=default_argv(models, idle_seconds=config.agent_idle_seconds), models=models, repo_root=config.repo_root)
		self.chat = ChatManager(config.repo_root, config.state_root, self.registry, self.monitor, self.agent, models=models,
			publish=self.broker.publish, enabled=config.agent_enabled, stage_root=config.stage_root)

	def _path_of_run(self, run_id: str) -> Optional[Path]:
		try:
			return self.registry.get(run_id).canonical_path
		except KeyError:
			return None

	def _run_of_path(self, path: Path) -> Optional[str]:
		run = self.registry.find(path)
		return run.run_id if run else None

	def _job_finished(self, job: JobRecord) -> None:
		if job.state == "succeeded":   # its outputs are new scene graph files: do not wait for the next poll
			self.monitor.request_scan()

	def start(self) -> None:
		"""Start the background work (the scene graph monitor); idempotent."""
		self.monitor.start()

	def stop(self) -> None:
		"""Stop the jobs, the monitor and every event stream; idempotent."""
		self.jobs.shutdown()
		self.monitor.stop()
		self.chat.stop()
		self.broker.close()

	def state_payload(self, run_id: str) -> Dict[str, Any]:
		"""What a page needs to catch up with a result: sent first on every new event stream and after a gap."""
		return {"graph": self.monitor.status(run_id), "jobs": self.jobs.state_for_run(run_id), "chat": self.chat.state(run_id)}

	def graph_payload(self, run_id: str) -> Dict[str, Any]:
		self.run(run_id)
		view = self.monitor.view(run_id)
		view["priority"] = list(GRAPH_PRIORITY)
		return view

	@property
	def allowed_hosts(self) -> Set[str]:
		return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

	@property
	def allowed_origins(self) -> Set[str]:
		return {f"http://{host}" for host in self.allowed_hosts}

	def run(self, run_id: str) -> OpenRun:
		if not _RUN_ID.match(run_id or ""):
			raise HttpError(404, "unknown run id", "run")
		try:
			return self.registry.get(run_id)
		except KeyError as error:
			raise HttpError(404, "unknown run id: open the result again", "run") from error

	def assets(self, run_id: str) -> RunAssets:
		run = self.run(run_id)
		with self._lock:
			cached = self._assets.get(run_id)
			if cached is None or cached.run is not run:
				cached = RunAssets(run)
				self._assets[run_id] = cached
			return cached

	def config_payload(self) -> Dict[str, Any]:
		return dict({
			"schema_version": SCHEMA_VERSION,
			"csrf_token": self.csrf_token,
			"limits": {"json_body_bytes": JSON_BODY_LIMIT},
			"output_root": str(self.config.output_root),
			"distro": self.config.distro,
		}, **self.chat.config_payload())

	def results_payload(self) -> Dict[str, Any]:
		return {"output_root": str(self.config.output_root), "results": [item.to_json() for item in self.registry.discover()]}

	def open_payload(self, body: Mapping[str, Any]) -> Dict[str, Any]:
		raw = body.get("path")
		if not isinstance(raw, str):
			raise HttpError(400, "send {\"path\": \"<absolute result directory>\"}", "request")
		try:
			run = self.registry.open(raw)
		except PathError as error:
			raise HttpError(400, str(error), "path") from error
		except ManifestError as error:
			raise HttpError(422, str(error), "manifest") from error
		return self.describe(run)

	def describe(self, run: OpenRun) -> Dict[str, Any]:
		assets = self.assets(run.run_id)
		dataset = run.manifest["dataset"]
		return {
			"run_id": run.run_id,
			"canonical_path": str(run.canonical_path),
			"title": run.manifest.get("title") or run.canonical_path.name,
			"dataset_kind": dataset["kind"],
			"sequence": dataset["sequence"],
			"bundle_version": assets.version,
		}

	def jobs_payload(self, run_id: str) -> Dict[str, Any]:
		self.run(run_id)
		return self.jobs.state_for_run(run_id)

	def start_job(self, run_id: str, body: Mapping[str, Any]) -> Dict[str, Any]:
		"""Start one allowlisted stage on an open result. The browser chooses the stage and whether to replace; the dataset
		kind comes from the result's own manifest, the path from the registry, and nothing else reaches the command."""
		run = self.run(run_id)
		unknown = sorted(set(body) - {"stage", "replace"})
		if unknown or not isinstance(body.get("stage"), str):
			raise HttpError(400, 'send {"stage": "postprocess" | "summarize" | "all", "replace": false}', "request")
		try:
			stage = JobStage(body["stage"])
		except ValueError as error:
			raise HttpError(400, "unknown stage: use postprocess, summarize or all", "stage") from error
		replace = body.get("replace", False)
		if not isinstance(replace, bool):
			raise HttpError(400, "replace must be true or false", "request")
		spec = JobSpec(run_id=run.run_id, result_path=run.canonical_path, dataset_kind=run.manifest["dataset"]["kind"], stage=stage, replace=replace)
		try:
			started = {"job": self.jobs.start(spec).to_json()}
		except JobError as error:
			raise HttpError(error.status, str(error), error.code) from error
		self.chat.release_for_job()   # an idle Agent worker gives its models back to the GPU for the job
		return started

	def chat_history(self, run_id: str) -> Dict[str, Any]:
		run = self.run(run_id)
		try:
			return self.chat.history(run)
		except ChatError as error:
			raise HttpError(error.status, error.message, error.code) from error

	def begin_chat(self, run_id: str, body: Mapping[str, Any]) -> Turn:
		"""Record a question of an open result; the turn streams the answer. The refusals are plain HTTP errors."""
		run = self.run(run_id)
		try:
			return self.chat.begin(run, body)
		except ChatError as error:
			raise HttpError(error.status, error.message, error.code) from error

	def clear_chat(self, run_id: str) -> None:
		run = self.run(run_id)
		try:
			self.chat.clear(run)
		except ChatError as error:
			raise HttpError(error.status, error.message, error.code) from error

	def cancel_job(self, job_id: str) -> Dict[str, Any]:
		try:
			return {"job": self.jobs.cancel(job_id).to_json()}
		except JobError as error:
			raise HttpError(error.status, str(error), error.code) from error

	def close(self, run_id: str) -> None:
		run = self.run(run_id)
		self.registry.close(run_id)
		with self._lock:
			self._assets.pop(run_id, None)
		self.chat.forget(run)   # the worker may drop the result's cached graph; its conversation stays on disk
		self.broker.publish(run_id, "closed", {"reason": "closed"})   # other pages that show this result
		self.broker.forget(run_id)


class ReplayRequestHandler(BaseHTTPRequestHandler):
	server_version = "DAAAMReplay/1"
	protocol_version = "HTTP/1.1"
	service: ReplayService = None   # set by make_handler

	# ------------------------------------------------------------------ plumbing
	def log_message(self, format: str, *args: Any) -> None:   # only failures are worth a line
		return

	def log_failure(self, status: int, message: str) -> None:
		sys.stderr.write(f"[replay-web] {self.command} {urllib.parse.urlsplit(self.path).path} -> {status}: {message}\n")

	def do_GET(self) -> None:
		self.dispatch("GET")

	def do_POST(self) -> None:
		self.dispatch("POST")

	def do_DELETE(self) -> None:
		self.dispatch("DELETE")

	def do_PUT(self) -> None:
		self.dispatch("PUT")

	def do_PATCH(self) -> None:
		self.dispatch("PATCH")

	def dispatch(self, method: str) -> None:
		try:
			if self.headers.get("Host") not in self.service.allowed_hosts:
				raise HttpError(403, "this service answers only http://127.0.0.1 and http://localhost on its own port", "host")
			split = urllib.parse.urlsplit(self.path)
			segments = [urllib.parse.unquote(part) for part in split.path.split("/")[1:]]
			if method in ("POST", "DELETE", "PUT", "PATCH"):
				self.check_mutation(method)
			self.route(method, segments, urllib.parse.parse_qs(split.query))
		except HttpError as error:
			if error.status >= 500 or error.status == 403:
				self.log_failure(error.status, error.message)
			self.send_json(error.status, {"error": error.message, "code": error.code})
		except (BrokenPipeError, ConnectionResetError):
			self.close_connection = True
		except Exception as error:   # never take the service down for one request
			traceback.print_exc()
			try:
				self.send_json(500, {"error": f"internal error: {error}", "code": "internal"})
			except OSError:
				self.close_connection = True

	def check_mutation(self, method: str) -> None:
		if self.headers.get("Origin") not in self.service.allowed_origins:
			raise HttpError(403, "cross-origin request refused", "origin")
		token = self.headers.get("X-Replay-CSRF") or ""
		if not hmac.compare_digest(token.encode("utf-8"), self.service.csrf_token.encode("utf-8")):
			raise HttpError(403, "missing or wrong X-Replay-CSRF token (reload the page)", "csrf")

	def read_json(self) -> Dict[str, Any]:
		if self.headers.get("Transfer-Encoding"):
			self.close_connection = True
			raise HttpError(411, "send a Content-Length", "request")
		content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
		if content_type != "application/json":
			self.close_connection = True
			raise HttpError(415, "send application/json", "request")
		try:
			length = int(self.headers.get("Content-Length") or "0")
		except ValueError as error:
			raise HttpError(400, "bad Content-Length", "request") from error
		if length > JSON_BODY_LIMIT:
			self.close_connection = True
			raise HttpError(413, f"request body over {JSON_BODY_LIMIT} bytes", "request")
		raw = self.rfile.read(length) if length > 0 else b"{}"
		try:
			value = json.loads(raw.decode("utf-8"))
		except (UnicodeDecodeError, ValueError) as error:
			raise HttpError(400, f"request body is not JSON ({error})", "request") from error
		if not isinstance(value, dict):
			raise HttpError(400, "request body must be a JSON object", "request")
		return value

	def send_json(self, status: int, value: Optional[Mapping[str, Any]]) -> None:
		body = b"" if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
		self.send_response(status)
		if value is not None:
			self.send_header("Content-Type", "application/json; charset=utf-8")
		self.send_header("Content-Length", str(len(body)))
		self.send_header("Cache-Control", "no-store")
		self.send_header("X-Content-Type-Options", "nosniff")
		if self.close_connection:
			self.send_header("Connection", "close")
		self.end_headers()
		if body:
			self.wfile.write(body)

	def send_file(self, path: Path, content_type: str, cache: str, etag: Optional[str] = None, headers: Optional[Mapping[str, str]] = None) -> None:
		if etag and etag in [tag.strip() for tag in (self.headers.get("If-None-Match") or "").split(",")]:
			self.send_response(304)
			self.send_header("ETag", etag)
			self.send_header("Cache-Control", cache)
			self.send_header("Content-Length", "0")
			self.end_headers()
			return
		try:
			stream = path.open("rb")
		except OSError as error:
			raise HttpError(404, f"cannot read {path.name}", "missing") from error
		with stream:
			size = os.fstat(stream.fileno()).st_size
			self.send_response(200)
			self.send_header("Content-Type", content_type)
			self.send_header("Content-Length", str(size))
			self.send_header("Cache-Control", cache)
			self.send_header("X-Content-Type-Options", "nosniff")
			if etag:
				self.send_header("ETag", etag)
			for name, value in (headers or {}).items():
				self.send_header(name, value)
			self.end_headers()
			shutil.copyfileobj(stream, self.wfile, 1 << 16)

	# ------------------------------------------------------------------ routes
	def route(self, method: str, segments: List[str], query: Dict[str, List[str]]) -> None:
		if segments and segments[0] == "api":
			return self.route_api(method, segments[1:], query)
		if method != "GET":
			raise HttpError(405, "method not allowed", "method")
		if segments in ([""], []):
			self.send_response(302)
			self.send_header("Location", "/replay.html")
			self.send_header("Content-Length", "0")
			self.end_headers()
			return None
		if segments == ["replay.html"]:
			page = self.service.config.repo_root / "replay.html"
			return self.send_file(page, "text/html; charset=utf-8", "no-cache", self.static_etag(page), {
				"Content-Security-Policy": CONTENT_SECURITY_POLICY,
				"Referrer-Policy": "no-referrer",
				"X-Frame-Options": "DENY",
			})
		if len(segments) == 2 and segments[0] == "static" and _STATIC_NAME.match(segments[1]):
			suffix = Path(segments[1]).suffix.lower()
			candidate = STATIC_DIR / segments[1]
			if suffix in STATIC_TYPES and candidate.is_file() and candidate.resolve().parent == STATIC_DIR:
				return self.send_file(candidate, STATIC_TYPES[suffix], "no-cache", self.static_etag(candidate))
		raise HttpError(404, "not found", "missing")

	@staticmethod
	def static_etag(path: Path) -> str:
		stat = path.stat()
		return '"%x-%x"' % (stat.st_mtime_ns, stat.st_size)

	def route_api(self, method: str, segments: List[str], query: Dict[str, List[str]]) -> None:
		service = self.service
		if segments == ["config"] and method == "GET":
			return self.send_json(200, service.config_payload())
		if segments == ["results"] and method == "GET":
			return self.send_json(200, service.results_payload())
		if segments == ["runs", "open"]:
			if method != "POST":
				raise HttpError(405, "method not allowed", "method")
			return self.send_json(200, service.open_payload(self.read_json()))
		if segments == ["events"]:
			if method != "GET":
				raise HttpError(405, "method not allowed", "method")
			return self.serve_shared_events(query.get("run", []))
		if len(segments) == 3 and segments[0] == "jobs" and segments[2] == "cancel":
			if method != "POST":
				raise HttpError(405, "method not allowed", "method")
			self.read_json()
			return self.send_json(200, service.cancel_job(segments[1]))
		if len(segments) >= 2 and segments[0] == "runs":
			run_id, rest = segments[1], segments[2:]
			if not rest:
				if method != "DELETE":
					raise HttpError(405, "method not allowed", "method")
				service.close(run_id)
				return self.send_json(204, None)
			if rest == ["jobs"]:
				if method == "POST":
					return self.send_json(201, service.start_job(run_id, self.read_json()))
				if method == "GET":
					return self.send_json(200, service.jobs_payload(run_id))
				raise HttpError(405, "method not allowed", "method")
			if rest == ["chat"]:
				if method == "GET":
					return self.send_json(200, service.chat_history(run_id))
				if method == "POST":
					return self.stream_chat(run_id, self.read_json())
				if method == "DELETE":
					service.clear_chat(run_id)
					return self.send_json(204, None)
				raise HttpError(405, "method not allowed", "method")
			if method != "GET":
				raise HttpError(405, "method not allowed", "method")
			if rest == ["manifest"]:
				return self.send_json(200, service.run(run_id).manifest)
			if rest == ["graph"]:
				return self.send_json(200, service.graph_payload(run_id))
			if rest == ["events"]:
				service.run(run_id)
				return self.serve_events([run_id], [])
			if len(rest) == 2 and rest[0] == "timeline":
				return self.send_chunk(run_id, rest[1], query)
			if len(rest) == 2 and rest[0] == "images":
				return self.send_image(run_id, rest[1], query)
			if rest == ["reference"]:
				return self.send_reference(run_id, query)
		raise HttpError(404, "not found", "missing")

	def serve_shared_events(self, requested: List[str]) -> None:
		"""One stream for several open results: the page keeps a single connection however many tabs it has."""
		runs = list(dict.fromkeys(requested))
		if not runs or len(runs) > MAX_STREAM_RUNS or not all(_RUN_ID.match(run_id) for run_id in runs):
			raise HttpError(400, f"name between 1 and {MAX_STREAM_RUNS} open results as ?run=<run id>&run=<run id>", "request")
		known = []
		for run_id in runs:
			try:
				self.service.registry.get(run_id)
				known.append(run_id)
			except KeyError:
				pass
		self.serve_events(known, [run_id for run_id in runs if run_id not in known])

	def serve_events(self, run_ids: List[str], unknown: List[str]) -> None:
		"""Server-sent events for the given results, until the client leaves or the service stops.

		A new stream starts with each result's state; a reconnecting page that sends the Last-Event-ID it saw gets the
		events it missed instead when the history still has them. Results the service does not know are reported once
		as `closed` (the service restarted, or another page closed them).
		"""
		service = self.service
		after = service.broker.parse_event_id(self.headers.get("Last-Event-ID"))
		self.close_connection = True
		self.send_response(200)
		self.send_header("Content-Type", "text/event-stream; charset=utf-8")
		self.send_header("Cache-Control", "no-store")
		self.send_header("X-Content-Type-Options", "nosniff")
		self.send_header("Connection", "close")
		self.end_headers()
		self.wfile.write(f"retry: {SSE_RETRY_MS}\n\n".encode("ascii"))
		for run_id in unknown:
			data = json.dumps({"run_id": run_id, "reason": "unknown"}, separators=(",", ":"))
			self.wfile.write(f"event: closed\ndata: {data}\n\n".encode("utf-8"))
		self.wfile.flush()
		with service.broker.subscribe(run_ids, after_id=after) as subscription:
			while True:
				try:
					event = subscription.get(timeout=service.config.sse_heartbeat)
				except queue.Empty:
					self.wfile.write(b": keep-alive\n\n")
					self.wfile.flush()
					continue
				except SubscriptionClosed:
					return
				self.wfile.write(encode_sse(event, service.broker.epoch))
				self.wfile.flush()

	def stream_chat(self, run_id: str, body: Mapping[str, Any]) -> None:
		"""One question as server-sent events (`turn`, `progress`, `delta`, then `complete` or `error`). Everything that can be
		refused is refused first, as a plain JSON error; the stream starts with the first event. A page that leaves does not
		stop the turn: its answer is stored all the same."""
		turn = self.service.begin_chat(run_id, body)
		started = False

		def emit(name: str, data: Dict[str, Any]) -> None:
			nonlocal started
			if not started:
				started = True
				self.close_connection = True
				self.send_response(200)
				self.send_header("Content-Type", "text/event-stream; charset=utf-8")
				self.send_header("Cache-Control", "no-store")
				self.send_header("X-Content-Type-Options", "nosniff")
				self.send_header("Connection", "close")
				self.end_headers()
			self.wfile.write(f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'), ensure_ascii=False)}\n\n".encode("utf-8"))
			self.wfile.flush()

		try:
			turn.run(emit)
		except ChatError as error:   # raised only while nothing was streamed: a plain response is still possible
			raise HttpError(error.status, error.message, error.code) from error

	def bundle_member(self, assets: RunAssets, relative: str, query: Dict[str, List[str]]) -> Path:
		version = query.get("v", [None])[0]
		if version is not None and version != assets.version:
			raise HttpError(404, "this bundle version is no longer open (reload the result)", "stale")
		try:
			return resolve_bundle_member(assets.run.bundle_root, relative)
		except ManifestError as error:
			raise HttpError(404, str(error), "missing") from error

	def send_chunk(self, run_id: str, raw_index: str, query: Dict[str, List[str]]) -> None:
		assets = self.service.assets(run_id)
		chunks = assets.run.manifest["chunks"]
		if not raw_index.isdigit() or int(raw_index) >= len(chunks):
			raise HttpError(404, f"no timeline chunk {raw_index}", "missing")
		chunk = chunks[int(raw_index)]
		path = self.bundle_member(assets, chunk["path"], query)
		self.send_file(path, "application/json; charset=utf-8", IMMUTABLE, '"%s"' % chunk["sha256"], {"Content-Encoding": "gzip"})

	def send_image(self, run_id: str, asset: str, query: Dict[str, List[str]]) -> None:
		assets = self.service.assets(run_id)
		suffix = Path(asset).suffix.lower()
		if asset not in assets.images or suffix not in IMAGE_TYPES:
			raise HttpError(404, "not an image of this replay bundle", "missing")
		path = self.bundle_member(assets, asset, query)
		etag = '"%s-%s"' % (assets.version, hashlib.sha1(asset.encode("utf-8")).hexdigest()[:12])
		self.send_file(path, IMAGE_TYPES[suffix], IMMUTABLE, etag)

	def send_reference(self, run_id: str, query: Dict[str, List[str]]) -> None:
		assets = self.service.assets(run_id)
		if assets.reference is None:
			raise HttpError(404, "this replay bundle has no reference mesh", "missing")
		path = self.bundle_member(assets, assets.reference, query)
		reference = assets.run.manifest["reference_mesh"]
		etag = '"%s"' % reference.get("sha256", assets.version)
		self.send_file(path, "application/json; charset=utf-8", IMMUTABLE, etag, {"Content-Encoding": "gzip"})


def make_handler(service: ReplayService) -> type:
	return type("BoundReplayRequestHandler", (ReplayRequestHandler,), {"service": service})


class ReplayHTTPServer(ThreadingHTTPServer):
	daemon_threads = True

	def __init__(self, address, handler, service: ReplayService):
		super().__init__(address, handler)
		self.service = service

	def serve_forever(self, poll_interval: float = 0.5) -> None:
		try:
			self.service.start()
			super().serve_forever(poll_interval)
		finally:
			self.server_close()

	def server_close(self) -> None:
		# Stop accepting requests before waiting for jobs/monitor cleanup.
		try:
			super().server_close()
		finally:
			self.service.stop()


def create_server(config: ServerConfig, registry: Optional[RunRegistry] = None, jobs: Optional[JobManager] = None, agent: Any = None) -> ReplayHTTPServer:
	"""A bound (not yet serving) server; the port is the configured one, or a free one for port 0. Tests pass a `jobs`
	manager that runs no docker and an `agent` client that starts no worker."""
	if config.host not in LOOPBACK_HOSTS:
		raise ValueError(f"the replay service binds a loopback address only (127.0.0.1 or localhost), not {config.host}")
	service = ReplayService(config, registry, jobs, agent)
	server = ReplayHTTPServer((LOOPBACK_HOSTS[config.host], config.port), make_handler(service), service)
	service.port = server.server_address[1]
	return server


def open_browser(url: str) -> None:
	"""Best effort: the Windows browser when running in WSL, else nothing (the URL is printed anyway)."""
	for command in (["wslview", url], ["explorer.exe", url]):
		if shutil.which(command[0]):
			subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
			return


def main(argv: Optional[List[str]] = None) -> int:
	parser = argparse.ArgumentParser(description="Serve the DAAAM replay workspace (replay.html) on a loopback port")
	parser.add_argument("--host", default="127.0.0.1", help="127.0.0.1 (default) or localhost; other addresses are refused")
	parser.add_argument("--port", type=int, default=8765)
	parser.add_argument("--output-root", default=str(REPO_ROOT / "output"), help="results listed by default (searched a few levels deep)")
	parser.add_argument("--state-root", default=str(REPO_ROOT / ".replay-web"), help="local service state (jobs, chat history, cache)")
	parser.add_argument("--scan-depth", type=int, default=4)
	parser.add_argument("--graph-interval", type=float, default=2.0, help="seconds between looks at the scene graph files of the open results")
	parser.add_argument("--max-jobs", type=int, default=1, help="processing jobs that may run at once (default 1: the others wait)")
	parser.add_argument("--agent-models", default=None, help="models a question may name, comma separated (default: $REPLAY_AGENT_MODELS, else gpt-5-mini)")
	parser.add_argument("--no-agent", action="store_true", help="switch the question panel off: no question is accepted and no worker is started")
	parser.add_argument("--agent-idle-seconds", type=float, default=None,
		help="the Agent worker ends after this many seconds without a question (default 900; 0: never)")
	parser.add_argument("--open", action="store_true", help="also open the page in the Windows browser")
	args = parser.parse_args(argv)
	config = ServerConfig(
		repo_root=REPO_ROOT, output_root=Path(args.output_root), state_root=Path(args.state_root),
		distro=os.environ.get("WSL_DISTRO_NAME", ""), host=args.host, port=args.port, scan_depth=args.scan_depth,
		graph_interval=max(0.2, args.graph_interval), max_jobs=max(1, args.max_jobs),
		agent_models=allowed_models({"REPLAY_AGENT_MODELS": args.agent_models}) if args.agent_models else None,
		agent_enabled=not args.no_agent, agent_idle_seconds=args.agent_idle_seconds,
	)
	try:
		server = create_server(config)
	except ValueError as error:
		print(f"[replay-web] {error}", file=sys.stderr)
		return 2
	except OSError as error:
		print(f"[replay-web] cannot listen on {args.host}:{args.port} ({error}); is another `just replay-web` running? Try: just replay-web port={args.port + 1}", file=sys.stderr)
		return 1
	url = f"http://{args.host}:{server.server_address[1]}/replay.html"
	previous_signals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

	def stop_signal(signum, frame):
		# Explicit handlers also recover from SIG_IGN inherited from a shell
		# background launch. Repeated interrupts must not break durable cleanup.
		for sig in previous_signals:
			signal.signal(sig, signal.SIG_IGN)
		raise KeyboardInterrupt

	for sig in previous_signals:
		signal.signal(sig, stop_signal)
	try:
		print(f"[replay-web] replay workspace: {url}", flush=True)
		print(f"[replay-web] results listed from {config.output_root}; any other result directory can be opened by its path. Ctrl+C stops.", flush=True)
		if args.open:
			open_browser(url)
		server.serve_forever()
	except KeyboardInterrupt:
		print("\n[replay-web] stopped", flush=True)
	finally:
		try:
			server.server_close()
		finally:
			for sig, handler in previous_signals.items():
				signal.signal(sig, handler)
	return 0


if __name__ == "__main__":
	sys.exit(main())
