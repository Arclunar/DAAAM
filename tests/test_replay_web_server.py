import gzip
import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from scripts.replay_web.artifacts import GRAPH_PRIORITY
from scripts.replay_web.server import ServerConfig, create_server
from tests.replay_fixtures import write_graph, write_minimal_bundle


REPO = Path(__file__).resolve().parents[1]
DISTRO = "Ubuntu-20.04"


class ReplayServerTestCase(unittest.TestCase):
	"""A real server on an ephemeral loopback port, serving the repository's application and temporary results."""

	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.root = Path(self.temp.name).resolve()
		self.output = self.root / "output"
		self.result = write_minimal_bundle(self.output / "tum_replay" / "out_20261017_000000", title="TUM fixture")
		self.coda = write_minimal_bundle(self.root / "elsewhere" / "out_coda", dataset_kind="coda", sequence="0", title="CODa fixture")
		self.server = create_server(self.server_config())
		self.port = self.server.server_address[1]
		self.base = f"http://127.0.0.1:{self.port}"
		self.origin = self.base
		thread = threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True)
		thread.start()
		self.addCleanup(thread.join, 5)
		self.addCleanup(self.server.server_close)
		self.addCleanup(self.server.shutdown)
		self.csrf = self.get_json("/api/config")["csrf_token"]

	def tearDown(self) -> None:
		self.temp.cleanup()

	def server_config(self) -> ServerConfig:
		return ServerConfig(repo_root=REPO, output_root=self.output, state_root=self.root / "state", distro=DISTRO, port=0,
			graph_interval=0.05, sse_heartbeat=0.1)

	def request(self, method: str, path: str, body=None, headers=None):
		connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
		payload = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
		all_headers = {"Host": f"127.0.0.1:{self.port}"}
		if payload is not None:
			all_headers["Content-Type"] = "application/json"
		all_headers.update(headers or {})
		connection.request(method, path, body=payload, headers=all_headers)
		response = connection.getresponse()
		data = response.read()
		connection.close()
		return response.status, {k.lower(): v for k, v in response.getheaders()}, data

	def get_json(self, path: str):
		status, headers, data = self.request("GET", path)
		self.assertEqual(status, 200, data)
		self.assertTrue(headers["content-type"].startswith("application/json"), headers)
		self.assertEqual(headers["x-content-type-options"], "nosniff")
		return json.loads(data)

	def mutate(self, method: str, path: str, body=None, csrf=True, origin=True, extra=None):
		headers = {}
		if csrf:
			headers["X-Replay-CSRF"] = self.csrf
		if origin:
			headers["Origin"] = self.origin
		headers.update(extra or {})
		status, response_headers, data = self.request(method, path, body if body is not None or method != "POST" else {}, headers)
		return status, json.loads(data) if data else None

	def open_result(self, path: Path) -> dict:
		status, opened = self.mutate("POST", "/api/runs/open", {"path": str(path)})
		self.assertEqual(status, 200, opened)
		return opened

	def wait_for(self, predicate: Callable[[], Any], timeout: float = 10.0, message: str = "condition") -> Any:
		deadline = time.monotonic() + timeout
		while True:
			value = predicate()
			if value:
				return value
			if time.monotonic() > deadline:
				self.fail(f"timed out waiting for {message}")
			time.sleep(0.02)

	def stream(self, path: str, headers: Optional[Dict[str, str]] = None) -> "SseStream":
		stream = SseStream(self.port, path, headers)
		self.addCleanup(stream.close)
		return stream


class SseStream:
	"""A minimal server-sent events client over http.client, enough to read frames and comments in order."""

	def __init__(self, port: int, path: str, headers: Optional[Dict[str, str]] = None, timeout: float = 5.0):
		self.connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
		self.connection.request("GET", path, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
		self.response = self.connection.getresponse()
		self.pending: Dict[str, Any] = {}

	def close(self) -> None:
		self.response.close()   # the response owns the socket of a stream that ends with the connection
		self.connection.close()

	def line(self) -> Optional[str]:
		"""The next line without its newline; None at the end of the stream."""
		raw = self.response.readline()
		return raw.decode("utf-8").rstrip("\n") if raw else None

	def frame(self, comments: bool = False) -> Optional[Dict[str, Any]]:
		"""The next complete event {id, event, data (parsed), retry} or, with `comments`, a {"comment": text}."""
		while True:
			line = self.line()
			if line is None:
				return None
			if line == "":
				if "event" in self.pending or "data" in self.pending:
					frame, self.pending = self.pending, {}
					return frame
				self.pending = {}
				continue
			if line.startswith(":"):
				if comments:
					return {"comment": line[1:].strip()}
				continue
			name, _, value = line.partition(":")
			value = value[1:] if value.startswith(" ") else value
			self.pending[name] = json.loads(value) if name == "data" else value

	def event(self) -> Dict[str, Any]:
		frame = self.frame()
		if frame is None:
			raise AssertionError("the stream ended")
		return frame

	def events_until(self, event_type: str, limit: int = 20) -> Dict[str, Any]:
		for _ in range(limit):
			frame = self.event()
			if frame.get("event") == event_type:
				return frame
		raise AssertionError(f"no {event_type} event within {limit} frames")


class ReplayServerContractTest(ReplayServerTestCase):
	def test_config_discovery_open_and_manifest(self) -> None:
		config = self.get_json("/api/config")
		self.assertEqual(config["schema_version"], 1)
		self.assertGreaterEqual(len(config["csrf_token"]), 32)
		self.assertEqual(config["limits"]["json_body_bytes"], 64 * 1024)
		results = self.get_json("/api/results")
		self.assertEqual([item["path"] for item in results["results"]], [str(self.result)])
		self.assertEqual(results["results"][0]["title"], "TUM fixture")
		opened = self.open_result(self.result)
		self.assertNotIn(str(self.result), opened["run_id"])
		self.assertEqual(opened["canonical_path"], str(self.result))
		self.assertEqual(opened["dataset_kind"], "tum")
		self.assertRegex(opened["bundle_version"], r"^[0-9a-f]{16}$")
		self.assertEqual(self.get_json(f"/api/runs/{opened['run_id']}/manifest")["dataset"]["kind"], "tum")
		other = self.open_result(self.coda)
		self.assertNotEqual(other["run_id"], opened["run_id"])
		self.assertEqual(self.get_json(f"/api/runs/{other['run_id']}/manifest")["dataset"]["sequence"], "0")

	def test_windows_and_unc_forms_reach_the_same_result(self) -> None:
		first = self.open_result(self.result)
		unc = "\\\\wsl.localhost\\" + DISTRO + str(self.result).replace("/", "\\")
		status, second = self.mutate("POST", "/api/runs/open", {"path": unc})
		self.assertEqual(status, 200, second)
		self.assertEqual(second["run_id"], first["run_id"])

	def test_mutations_need_the_csrf_token_and_the_page_origin(self) -> None:
		body = {"path": str(self.result)}
		self.assertEqual(self.mutate("POST", "/api/runs/open", body, csrf=False)[0], 403)
		self.assertEqual(self.mutate("POST", "/api/runs/open", body, origin=False)[0], 403)
		self.assertEqual(self.mutate("POST", "/api/runs/open", body, extra={"X-Replay-CSRF": "wrong"})[0], 403)
		self.origin = "http://evil.example"
		self.assertEqual(self.mutate("POST", "/api/runs/open", body)[0], 403)
		self.origin = f"http://localhost:{self.port}"
		self.assertEqual(self.mutate("POST", "/api/runs/open", body)[0], 200)
		status, _, _ = self.request("POST", "/api/runs/open", b"path=x", {"Content-Type": "text/plain", "X-Replay-CSRF": self.csrf, "Origin": self.base})
		self.assertEqual(status, 415)
		opened = self.open_result(self.result)
		self.assertEqual(self.mutate("DELETE", f"/api/runs/{opened['run_id']}", csrf=False)[0], 403)

	def test_requests_for_another_host_name_are_refused(self) -> None:
		status, _, _ = self.request("GET", "/api/config", headers={"Host": f"evil.example:{self.port}"})
		self.assertEqual(status, 403, "a DNS-rebound page must not read the token or the result list")
		status, _, _ = self.request("GET", "/api/config", headers={"Host": f"localhost:{self.port}"})
		self.assertEqual(status, 200)

	def test_bad_request_bodies_are_rejected(self) -> None:
		status, _, _ = self.request("POST", "/api/runs/open", b"{", {"X-Replay-CSRF": self.csrf, "Origin": self.base})
		self.assertEqual(status, 400)
		status, _, _ = self.request("POST", "/api/runs/open", json.dumps({"path": "x" * 70000}).encode(), {"X-Replay-CSRF": self.csrf, "Origin": self.base})
		self.assertEqual(status, 413)
		self.assertEqual(self.mutate("POST", "/api/runs/open", {"nopath": 1})[0], 400)

	def test_open_errors_name_the_problem_and_change_nothing(self) -> None:
		status, error = self.mutate("POST", "/api/runs/open", {"path": "relative/run"})
		self.assertEqual(status, 400)
		self.assertIn("absolute", error["error"])
		plain = self.root / "plain"
		plain.mkdir()
		status, error = self.mutate("POST", "/api/runs/open", {"path": str(plain)})
		self.assertEqual(status, 422)
		self.assertIn(str(plain / "replay" / "manifest.json"), error["error"])
		self.assertEqual(error["code"], "manifest")

	def test_unknown_runs_are_not_found(self) -> None:
		for path in ("/api/runs/nope/manifest", "/api/runs/nope/timeline/0", "/api/runs/nope/images/x.jpg", "/api/runs/nope/reference"):
			with self.subTest(path=path):
				self.assertEqual(self.request("GET", path)[0], 404)
		self.assertEqual(self.mutate("DELETE", "/api/runs/nope")[0], 404)

	def test_closing_releases_only_the_server_state(self) -> None:
		opened = self.open_result(self.result)
		self.assertEqual(self.mutate("DELETE", f"/api/runs/{opened['run_id']}")[0], 204)
		self.assertEqual(self.request("GET", f"/api/runs/{opened['run_id']}/manifest")[0], 404)
		self.assertTrue((self.result / "replay" / "manifest.json").is_file())


class ReplayServerAssetTest(ReplayServerTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.opened = self.open_result(self.result)
		self.run = f"/api/runs/{self.opened['run_id']}"
		self.manifest = self.get_json(self.run + "/manifest")

	def test_timeline_chunks_are_gzip_json_with_immutable_validators(self) -> None:
		chunk = self.manifest["chunks"][1]
		status, headers, data = self.request("GET", f"{self.run}/timeline/1?v={self.opened['bundle_version']}")
		self.assertEqual(status, 200)
		self.assertEqual(headers["content-encoding"], "gzip")
		self.assertTrue(headers["content-type"].startswith("application/json"))
		self.assertIn("immutable", headers["cache-control"])
		self.assertEqual(headers["etag"], '"%s"' % chunk["sha256"])
		self.assertEqual(int(headers["content-length"]), chunk["bytes"])
		self.assertEqual(data, (self.result / "replay" / chunk["path"]).read_bytes())
		self.assertEqual(json.loads(gzip.decompress(data))["snapshots"][0]["n"], 2)
		status, _, data = self.request("GET", f"{self.run}/timeline/1", headers={"If-None-Match": headers["etag"]})
		self.assertEqual((status, data), (304, b""))
		for index in ("3", "-1", "x", "1.0"):
			with self.subTest(index=index):
				self.assertEqual(self.request("GET", f"{self.run}/timeline/{index}")[0], 404)

	def test_a_stale_bundle_version_is_not_served(self) -> None:
		self.assertEqual(self.request("GET", f"{self.run}/timeline/0?v=0000000000000000")[0], 404)

	def test_images_are_served_only_from_the_manifest(self) -> None:
		asset = self.manifest["images"]["timeline"][0]["rgb"]
		status, headers, data = self.request("GET", f"{self.run}/images/{urllib.parse.quote(asset, safe='')}")
		self.assertEqual(status, 200)
		self.assertEqual(headers["content-type"], "image/jpeg")
		self.assertIn("immutable", headers["cache-control"])
		self.assertEqual(data, (self.result / "replay" / asset).read_bytes())
		source = self.manifest["images"]["descriptions"]["2"]["path"]
		self.assertEqual(self.request("GET", f"{self.run}/images/{urllib.parse.quote(source, safe='')}")[0], 200)
		for asset in ("/etc/passwd", "../../../etc/passwd", "manifest.json", "timeline/000000.json.gz", "images/timeline/rgb/../../manifest.json", "%2Fetc%2Fpasswd"):
			with self.subTest(asset=asset):
				status, _, data = self.request("GET", f"{self.run}/images/{urllib.parse.quote(asset, safe='')}")
				self.assertEqual(status, 404)
				self.assertNotIn(b"root:", data)
		for raw in ("/api/runs/%s/images/..%%2F..%%2F..%%2Fetc%%2Fpasswd" % self.opened["run_id"], "/static/..%2F..%2F..%2F..%2Fetc%2Fpasswd", "/static/../server.py", "/etc/passwd", "/../etc/passwd"):
			with self.subTest(raw=raw):
				status, _, data = self.request("GET", raw)
				self.assertIn(status, (400, 404))
				self.assertNotIn(b"root:", data)

	def test_a_bundle_without_a_reference_has_none_to_serve(self) -> None:
		self.assertEqual(self.request("GET", f"{self.run}/reference")[0], 404)


class ReplayServerGraphTest(ReplayServerTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.opened = self.open_result(self.result)
		self.run = f"/api/runs/{self.opened['run_id']}"
		self.wait_for(lambda: self.get_json(self.run + "/graph")["scanned"], message="the first scan")

	def test_a_result_without_graph_files_reports_that_and_the_search_order(self) -> None:
		graph = self.get_json(self.run + "/graph")
		self.assertEqual((graph["scanned"], graph["version"], graph["document"]), (True, None, None))
		self.assertEqual(graph["priority"], list(GRAPH_PRIORITY))
		self.assertEqual({name: entry["state"] for name, entry in graph["artifacts"].items()}, {name: "absent" for name in GRAPH_PRIORITY})

	def test_the_active_graph_comes_with_its_provenance_and_nothing_private(self) -> None:
		write_graph(self.result / "dsg.json", atomic=True, objects=(7, 8))
		graph = self.wait_for(lambda: self.get_json(self.run + "/graph")["version"] and self.get_json(self.run + "/graph"), message="dsg.json")
		self.assertEqual(graph["version"]["name"], "dsg.json")
		self.assertEqual(graph["version"]["rank"], 3)
		self.assertRegex(graph["version"]["digest"], r"^[0-9a-f]{64}$")
		self.assertEqual(graph["version"]["size"], (self.result / "dsg.json").stat().st_size)
		self.assertEqual([item["id"] for item in graph["document"]["objects"]], ["O7", "O8"])
		write_graph(self.result / "clustered_dsg_with_summaries.json", atomic=True, objects=(7,), rooms=[("office", "a desk area")])
		graph = self.wait_for(lambda: (lambda data: data if data["version"] and data["version"]["rank"] == 0 else None)(self.get_json(self.run + "/graph")), message="promotion")
		self.assertEqual(graph["document"]["rooms"][0]["label"], "office")
		self.assertEqual(graph["artifacts"]["dsg.json"]["state"], "valid", "it was read when it was the active graph")
		self.assertEqual(graph["artifacts"]["clustered_dsg.json"]["state"], "absent")
		status, headers, body = self.request("GET", self.run + "/graph")
		self.assertEqual(headers["cache-control"], "no-store")
		text = body.decode("utf-8")
		self.assertNotIn(str(self.result), text, "no host path reaches the browser")
		self.assertNotIn("PRIVATE CHAIN OF THOUGHT", text)
		self.assertNotIn("sentence_embedding_feature", text)

	def test_an_invalid_newer_candidate_leaves_the_valid_graph_active(self) -> None:
		write_graph(self.result / "clustered_dsg.json", atomic=True)
		self.wait_for(lambda: self.get_json(self.run + "/graph")["version"], message="clustered_dsg.json")
		(self.result / "clustered_dsg_with_summaries.json").write_text("{", encoding="utf-8")
		graph = self.wait_for(lambda: (lambda data: data if data["artifacts"]["clustered_dsg_with_summaries.json"]["state"] == "invalid" else None)(self.get_json(self.run + "/graph")), message="the invalid state")
		self.assertEqual(graph["version"]["name"], "clustered_dsg.json")
		self.assertNotIn(str(self.result), json.dumps(graph))

	def test_the_graph_of_another_run_is_never_mixed_in(self) -> None:
		other = self.open_result(self.coda)
		write_graph(self.result / "dsg.json", atomic=True)
		self.wait_for(lambda: self.get_json(self.run + "/graph")["version"], message="dsg.json")
		self.wait_for(lambda: self.get_json(f"/api/runs/{other['run_id']}/graph")["scanned"], message="the other scan")
		self.assertIsNone(self.get_json(f"/api/runs/{other['run_id']}/graph")["version"])

	def test_unknown_runs_and_wrong_methods_are_refused(self) -> None:
		self.assertEqual(self.request("GET", "/api/runs/nope/graph")[0], 404)
		self.assertEqual(self.mutate("POST", self.run + "/graph")[0], 405)
		self.assertEqual(self.mutate("DELETE", self.run + "/graph")[0], 405)


class ReplayServerEventsTest(ReplayServerTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.opened = self.open_result(self.result)
		self.run_id = self.opened["run_id"]
		self.run = f"/api/runs/{self.run_id}"
		self.wait_for(lambda: self.get_json(self.run + "/graph")["scanned"], message="the first scan")

	def test_a_stream_starts_with_the_state_and_reports_every_change_of_the_active_graph(self) -> None:
		stream = self.stream(self.run + "/events")
		self.assertEqual(stream.response.status, 200)
		self.assertTrue(stream.response.getheader("Content-Type").startswith("text/event-stream"))
		self.assertEqual(stream.response.getheader("Cache-Control"), "no-store")
		self.assertEqual(stream.response.getheader("X-Content-Type-Options"), "nosniff")
		state = stream.event()
		self.assertEqual(state["event"], "state")
		self.assertEqual(state["data"]["run_id"], self.run_id)
		self.assertEqual((state["data"]["graph"]["scanned"], state["data"]["graph"]["graph"]), (True, None))
		write_graph(self.result / "dsg.json", atomic=True)
		event = stream.event()
		self.assertEqual((event["event"], event["data"]["reason"], event["data"]["version"]["name"]), ("graph", "activated", "dsg.json"))
		self.assertEqual(event["data"]["run_id"], self.run_id)
		write_graph(self.result / "clustered_dsg.json", atomic=True, marker="clustered")
		event = stream.events_until("graph")
		self.assertEqual((event["data"]["reason"], event["data"]["version"]["name"], event["data"]["previous"]["name"]), ("promoted", "clustered_dsg.json", "dsg.json"))
		(self.result / "clustered_dsg_with_summaries.json").write_text("{", encoding="utf-8")
		event = stream.events_until("artifact-error")
		self.assertEqual(event["data"]["name"], "clustered_dsg_with_summaries.json")
		self.assertEqual(event["data"]["active"]["name"], "clustered_dsg.json")
		self.assertNotIn(str(self.result), json.dumps(event["data"]))
		(self.result / "clustered_dsg.json").unlink()
		event = stream.events_until("graph")
		self.assertEqual((event["data"]["reason"], event["data"]["version"]["name"]), ("demoted", "dsg.json"))

	def test_resuming_with_the_last_event_id_replays_only_what_was_missed(self) -> None:
		first = self.stream(self.run + "/events")
		self.assertEqual(first.event()["event"], "state")
		write_graph(self.result / "dsg.json", atomic=True)
		seen = first.event()
		first.close()
		write_graph(self.result / "clustered_dsg.json", atomic=True)
		self.wait_for(lambda: (self.get_json(self.run + "/graph")["version"] or {}).get("name") == "clustered_dsg.json", message="promotion")
		resumed = self.stream(self.run + "/events", {"Last-Event-ID": seen["id"]})
		event = resumed.event()
		self.assertEqual((event["event"], event["data"]["reason"]), ("graph", "promoted"), "a continuous history needs no state")
		stranger = self.stream(self.run + "/events", {"Last-Event-ID": "another-service.1"})
		self.assertEqual(stranger.event()["event"], "state")

	def test_an_idle_stream_sends_heartbeat_comments(self) -> None:
		stream = self.stream(self.run + "/events")
		self.assertEqual(stream.event()["event"], "state")
		self.assertEqual(stream.frame(comments=True), {"comment": "keep-alive"})
		self.assertEqual(stream.frame(comments=True), {"comment": "keep-alive"})

	def test_a_disconnected_client_is_released(self) -> None:
		broker = self.server.service.broker
		stream = self.stream(self.run + "/events")
		stream.event()
		self.assertEqual(broker.subscriber_count(), 1)
		stream.close()
		self.wait_for(lambda: broker.subscriber_count() == 0, message="the subscription to be released")

	def test_stopping_the_server_ends_open_streams(self) -> None:
		stream = self.stream(self.run + "/events")
		stream.event()
		started = time.monotonic()
		self.server.shutdown()
		self.server.server_close()
		self.assertIsNone(stream.frame(comments=True) if False else self.drain(stream), "the stream is closed by the service")
		self.assertLess(time.monotonic() - started, 5)

	def drain(self, stream: SseStream) -> Optional[Dict[str, Any]]:
		while True:
			frame = stream.frame(comments=True)
			if frame is None:
				return None

	def test_one_stream_can_carry_several_results(self) -> None:
		other = self.open_result(self.coda)
		stream = self.stream(f"/api/events?run={self.run_id}&run={other['run_id']}")
		states = {stream.event()["data"]["run_id"] for _ in range(2)}
		self.assertEqual(states, {self.run_id, other["run_id"]})
		write_graph(self.coda / "dsg.json", atomic=True)
		event = stream.events_until("graph")
		self.assertEqual(event["data"]["run_id"], other["run_id"], "events carry their run, and the other result sees nothing")
		write_graph(self.result / "dsg.json", atomic=True)
		event = stream.events_until("graph")
		self.assertEqual(event["data"]["run_id"], self.run_id)

	def test_unknown_results_in_a_shared_stream_are_reported_as_closed(self) -> None:
		stream = self.stream(f"/api/events?run={self.run_id}&run=nopenopenope")
		closed = stream.events_until("closed")
		self.assertEqual((closed["data"]["run_id"], closed["data"]["reason"]), ("nopenopenope", "unknown"))
		self.assertEqual(stream.events_until("state")["data"]["run_id"], self.run_id)

	def test_closing_a_result_tells_the_streams_that_carry_it(self) -> None:
		stream = self.stream(f"/api/events?run={self.run_id}")
		stream.events_until("state")
		self.assertEqual(self.mutate("DELETE", self.run)[0], 204)
		closed = stream.events_until("closed")
		self.assertEqual((closed["data"]["run_id"], closed["data"]["reason"]), (self.run_id, "closed"))

	def test_bad_stream_requests_are_refused(self) -> None:
		self.assertEqual(self.request("GET", "/api/events")[0], 400)
		self.assertEqual(self.request("GET", "/api/events?run=a%20b")[0], 400)
		self.assertEqual(self.request("GET", "/api/events?" + "&".join("run=abcdefghij%d" % n for n in range(65)))[0], 400)
		self.assertEqual(self.request("GET", "/api/runs/nope/events")[0], 404)
		self.assertEqual(self.mutate("POST", self.run + "/events")[0], 405)
		self.assertEqual(self.mutate("POST", "/api/events")[0], 405)
		status, _, _ = self.request("GET", self.run + "/events", headers={"Host": f"evil.example:{self.port}"})
		self.assertEqual(status, 403, "a DNS-rebound page cannot watch the results")


class ReplayServerApplicationTest(ReplayServerTestCase):
	def test_the_single_root_page_and_its_static_files(self) -> None:
		status, headers, data = self.request("GET", "/")
		self.assertIn(status, (301, 302, 307, 308))
		self.assertEqual(headers["location"], "/replay.html")
		status, headers, data = self.request("GET", "/replay.html")
		self.assertEqual(status, 200)
		self.assertTrue(headers["content-type"].startswith("text/html"))
		self.assertIn("default-src 'self'", headers["content-security-policy"])
		self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
		page = data.decode("utf-8")
		for element in ('id="result-path"', 'id="open-result"', 'id="result-list"', 'id="tabs"', 'id="workspace"', 'id="global-status"'):
			self.assertIn(element, page)
		self.assertIn('href="/static/replay.css"', page)
		self.assertIn("/static/api.js", page)
		self.assertIn('<script type="module" src="/static/app.js">', page)
		status, headers, data = self.request("GET", "/static/api.js")
		self.assertEqual(status, 200)
		self.assertTrue(headers["content-type"].startswith("text/javascript"))
		self.assertIn(b"export class ReplayApi", data)
		self.assertEqual(self.request("GET", "/static/replay.css")[1]["content-type"].split(";")[0], "text/css")
		for path in ("/static/missing.js", "/static/", "/scripts/replay_web/server.py", "/manifest.json", "/scripts/replay.py"):
			with self.subTest(path=path):
				self.assertEqual(self.request("GET", path)[0], 404)

	def test_only_loopback_hosts_are_accepted(self) -> None:
		for host in ("0.0.0.0", "192.168.1.5", "::"):
			with self.subTest(host=host), self.assertRaises(ValueError):
				create_server(ServerConfig(repo_root=REPO, output_root=self.output, state_root=self.root / "state", distro=DISTRO, host=host, port=0))

	def test_unsupported_methods_are_refused(self) -> None:
		self.assertEqual(self.request("PUT", "/api/runs/open", {"path": "x"}, {"X-Replay-CSRF": self.csrf, "Origin": self.base})[0], 405)


if __name__ == "__main__":
	unittest.main()
