"""The NDJSON protocol of the replay Agent worker and the worker itself, driven with a fake Agent (no models, no Docker)."""
import json
import os
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.replay_web import agent_protocol as protocol
from scripts.replay_web import agent_worker
from scripts.replay_web.agent_protocol import RequestError, parse_request
from scripts.replay_web.query import PLACES_LAYERS, DatasetContext, QueryConfigurationError
from tests.replay_fixtures import graph_document

DIGEST = "a" * 64
GRAPH_PATH = ".replay-web/cache/%s/%s.json" % ("b" * 64, DIGEST)
BACKGROUND_PATH = ".replay-web/cache/%s/%s.background.yaml" % ("b" * 64, DIGEST)
PRIVATE = "PRIVATE-REASONING-DO-NOT-LEAK"
SECRET = "sk-test-SECRET-KEY-0123456789"


def ask_request(**changes):
	request = {
		"id": "turn-1", "action": "ask", "result_key": "b" * 64, "graph_path": GRAPH_PATH, "graph_version": "2-" + "c" * 20,
		"dataset": {"kind": "tum", "sequence": "fr1_desk", "time_origin": 0.0}, "model": "gpt-5-mini",
		"question": "Where is the chair?", "conversation": [], "context": {},
	}
	request.update(changes)
	return request


def without(request, *names):
	return {key: value for key, value in request.items() if key not in names}


class ModelsTest(unittest.TestCase):
	def test_the_default_allowlist_is_the_current_query_model(self):
		self.assertEqual(protocol.allowed_models({}), ("gpt-5-mini",))
		self.assertEqual(protocol.DEFAULT_MODEL, "gpt-5-mini")

	def test_the_allowlist_comes_from_the_environment(self):
		models = protocol.allowed_models({"REPLAY_AGENT_MODELS": " gpt-5-mini, claude-sonnet-4-5;gpt-5-nano  gpt-5-mini "})

		self.assertEqual(models, ("gpt-5-mini", "claude-sonnet-4-5", "gpt-5-nano"), "split on commas, semicolons and blanks, once each, in order")

	def test_entries_that_are_not_model_names_are_dropped(self):
		models = protocol.allowed_models({"REPLAY_AGENT_MODELS": "ok-model, ../etc, $(x), " + "x" * 80 + ", -rf, .hidden, good.model:v1"})

		self.assertEqual(models, ("ok-model", "good.model:v1"))
		self.assertEqual(protocol.allowed_models({"REPLAY_AGENT_MODELS": " ,; "}), ("gpt-5-mini",))
		self.assertEqual(protocol.allowed_models({"REPLAY_AGENT_MODELS": "$(x)"}), ("gpt-5-mini",))


class ParseRequestTest(unittest.TestCase):
	def test_a_question_is_parsed_into_a_request(self):
		request = parse_request(ask_request(
			conversation=[{"role": "user", "content": "Find the red chair."}, {"role": "assistant", "content": "Beside the desk."}],
			context={"replay_time": 12.5, "selected_entity": "O7", "api_key": SECRET},
		))

		self.assertEqual(request.action, "ask")
		self.assertEqual((request.id, request.result_key, request.graph_path), ("turn-1", "b" * 64, GRAPH_PATH))
		self.assertEqual(request.dataset, DatasetContext("tum", "fr1_desk", 0.0, PLACES_LAYERS["tum"]))
		self.assertEqual((request.model, request.question), ("gpt-5-mini", "Where is the chair?"))
		self.assertEqual([turn["role"] for turn in request.conversation], ["user", "assistant"])
		self.assertEqual(request.context, {"replay_time": 12.5, "selected_entity": "O7"}, "only the replay fields reach the Agent")
		self.assertIsNone(request.background_path)

	def test_conversation_and_context_default_to_empty(self):
		request = parse_request(without(ask_request(), "conversation", "context"))

		self.assertEqual(request.conversation, ())
		self.assertEqual(request.context, {})

	def test_inspect_needs_the_graph_and_no_question(self):
		request = parse_request(without(ask_request(action="inspect"), "model", "question", "conversation", "context"))

		self.assertEqual((request.action, request.model, request.question), ("inspect", None, None))
		self.assertEqual(request.graph_path, GRAPH_PATH)

	def test_invalidate_needs_only_the_result(self):
		request = parse_request({"id": "inv-1", "action": "invalidate", "result_key": "b" * 64})

		self.assertEqual((request.action, request.result_key, request.graph_path), ("invalidate", "b" * 64, None))

	def test_a_coda_request_may_name_the_staged_background_file(self):
		request = parse_request(ask_request(
			dataset={"kind": "coda", "sequence": "0", "time_origin": 1673884185.589118}, background_path=BACKGROUND_PATH,
		))

		self.assertEqual(request.dataset.kind, "coda")
		self.assertEqual(request.background_path, BACKGROUND_PATH)

	def test_a_request_survives_its_wire_form(self):
		for raw in (
			ask_request(conversation=[{"role": "user", "content": "q"}], context={"replay_time": 1.0}),
			without(ask_request(action="inspect"), "model", "question", "conversation", "context"),
			{"id": "inv-1", "action": "invalidate", "result_key": "k"},
			ask_request(dataset={"kind": "coda", "sequence": "0", "time_origin": 1673884185.589118}, background_path=BACKGROUND_PATH),
		):
			request = parse_request(raw)
			wire = json.loads(json.dumps(request.to_wire()))
			self.assertEqual(parse_request(wire), request)

	def test_graph_paths_must_be_staged_snapshots(self):
		for bad in (
			"/etc/passwd", "/opt/daaam_ws/src/daaam/.replay-web/cache/k/x.json",
			".replay-web/cache/../../etc/passwd", ".replay-web/cache/k/../../x.json", "../.replay-web/cache/k/x.json",
			".replay-web/cache/k/sub/x.json", ".replay-web/cache/x.json", ".replay-web/history/k/x.json", "output/run/dsg.json",
			".replay-web/cache/k/x.yaml", ".replay-web/cache/k/x.json.bak", ".replay-web/cache/k/.json", ".replay-web/cache//x.json",
			".replay-web\\cache\\k\\x.json", ".replay-web/cache/k/x.json\x00", "file:///etc/passwd", "C:/x.json",
			".replay-web/cache/k k/x.json", ".replay-web/cache/./x.json", ".replay-web/cache/k/x\n.json", "", None, 7,
		):
			with self.subTest(path=bad):
				with self.assertRaises(RequestError):
					parse_request(ask_request(graph_path=bad))

	def test_background_paths_are_checked_the_same_way(self):
		for bad in ("/data/background.yaml", ".replay-web/cache/k/../x.yaml", ".replay-web/cache/k/x.json", "background_objects.yaml", 7):
			with self.subTest(path=bad):
				with self.assertRaises(RequestError):
					parse_request(ask_request(background_path=bad))

	def test_unknown_actions_and_fields_are_refused(self):
		for bad in (ask_request(action="shell"), ask_request(action=None), ask_request(command="rm -rf /"), without(ask_request(), "action")):
			with self.subTest(keys=sorted(bad)):
				with self.assertRaises(RequestError):
					parse_request(bad)

	def test_models_outside_the_allowlist_are_refused(self):
		with self.assertRaises(RequestError):
			parse_request(ask_request(model="gpt-5"), models=("gpt-5-mini",))
		self.assertEqual(parse_request(ask_request(model="gpt-5"), models=("gpt-5-mini", "gpt-5")).model, "gpt-5")
		for bad in ("", "gpt 5", "../x", "x" * 65, None, 5, "$(id)"):
			with self.subTest(model=bad):
				with self.assertRaises(RequestError):
					parse_request(ask_request(model=bad))

	def test_questions_are_bounded_and_not_blank(self):
		for bad in ("", "   \n ", None, 7, ["q"], "x" * (protocol.MAX_QUESTION_CHARS + 1), "é" * (protocol.MAX_QUESTION_BYTES // 2 + 1)):
			with self.subTest(question=str(bad)[:20]):
				with self.assertRaises(RequestError):
					parse_request(ask_request(question=bad))
		parse_request(ask_request(question="x" * protocol.MAX_QUESTION_CHARS))

	def test_conversation_turns_need_a_user_or_assistant_role_and_text(self):
		for bad in (
			[{"role": "system", "content": "x"}], [{"role": "boundary", "content": "x"}], [{"role": "user", "content": 5}],
			[{"role": "user"}], [{"content": "x"}], ["text"], "text", {"role": "user", "content": "x"},
			[{"role": "user", "content": "x" * 40000}, {"role": "assistant", "content": "y" * 40000}],
			[{"role": "user", "content": "x"}] * (protocol.MAX_TURNS + 1),
		):
			with self.subTest(conversation=str(bad)[:40]):
				with self.assertRaises(RequestError):
					parse_request(ask_request(conversation=bad))

	def test_a_conversation_may_be_as_large_as_the_agent_accepts(self):
		turns = [{"role": "user", "content": "x" * 30000}, {"role": "assistant", "content": "y" * 30000}]

		self.assertEqual(len(parse_request(ask_request(conversation=turns)).conversation), 2)

	def test_identifiers_are_plain_tokens(self):
		for field, bad in (
			("id", ""), ("id", "a b"), ("id", "x" * 65), ("id", None), ("id", "../x"),
			("result_key", ""), ("result_key", "a/b"), ("result_key", "x" * 129), ("result_key", ".."),
			("graph_version", ""), ("graph_version", "a b"), ("graph_version", "x" * 129), ("graph_version", "a/b"),
		):
			with self.subTest(field=field, value=str(bad)[:20]):
				with self.assertRaises(RequestError):
					parse_request(ask_request(**{field: bad}))

	def test_a_bad_dataset_is_refused(self):
		for bad in (
			{"kind": "kitti", "sequence": "0", "time_origin": 0.0}, {"kind": "tum", "sequence": "fr1_desk", "time_origin": 5.0},
			{"kind": "coda", "sequence": "0"}, "tum", None, ["tum"],
		):
			with self.subTest(dataset=bad):
				with self.assertRaises(RequestError):
					parse_request(ask_request(dataset=bad))

	def test_a_refusal_keeps_the_request_id_when_there_is_one(self):
		with self.assertRaises(RequestError) as caught:
			parse_request(ask_request(question=""))
		self.assertEqual(caught.exception.request_id, "turn-1")
		with self.assertRaises(RequestError) as caught:
			parse_request(ask_request(id="bad id", question=""))
		self.assertIsNone(caught.exception.request_id, "an id that is not a token is not echoed back")
		with self.assertRaises(RequestError) as caught:
			parse_request(["not", "an", "object"])
		self.assertIsNone(caught.exception.request_id)

	def test_a_request_line_is_decoded_and_bounded(self):
		line = json.dumps(ask_request()).encode("utf-8")
		self.assertEqual(protocol.parse_line(line).question, "Where is the chair?")
		self.assertEqual(protocol.parse_line(line.decode("utf-8")).id, "turn-1")
		for bad in (b"not json", b"[1]", b"\xff\xfe", b"", b"x" * (protocol.MAX_LINE_BYTES + 1)):
			with self.assertRaises(RequestError):
				protocol.parse_line(bad)


class EventsTest(unittest.TestCase):
	def test_progress_keeps_only_the_safe_fields(self):
		cases = [
			({"type": "iteration", "number": 2, "maximum": 15}, {"kind": "iteration", "number": 2, "maximum": 15}),
			({"type": "tool", "name": "get_matching_subjects"}, {"kind": "tool", "name": "get_matching_subjects"}),
			({"type": "final"}, {"kind": "final"}),
			({"type": "loading"}, {"kind": "loading"}),
			({"type": "tool", "name": "x", "arguments": '{"q": "secret"}', "result": {"big": "payload"}, "reasoning": PRIVATE}, {"kind": "tool", "name": "x"}),
			({"type": "final", "prompt": "hidden", "usage": 5}, {"kind": "final"}),
		]
		for progress, expected in cases:
			event = protocol.progress_event("turn-1", progress)
			self.assertEqual(event, dict(expected, id="turn-1", type="progress"))

	def test_unrecognised_or_malformed_progress_is_dropped(self):
		for progress in (
			{"type": "reasoning", "text": PRIVATE}, {"type": "tool"}, {"type": "tool", "name": "a b"}, {"type": "tool", "name": "x" * 65},
			{"type": "tool", "name": 7}, {"type": "iteration", "number": "2", "maximum": 15}, {"type": "iteration", "number": 0, "maximum": 15},
			{"type": "iteration", "number": 2}, {"type": "iteration", "number": True, "maximum": 3}, {}, None, "final", 3,
		):
			with self.subTest(progress=str(progress)):
				self.assertIsNone(protocol.progress_event("turn-1", progress))

	def test_events_are_single_lines_of_json(self):
		for event in (
			protocol.delta_event("t", "line one\nline two \u2028 é"), protocol.complete_event("t", answer="a\nb", model="m", graph_version="v"),
			protocol.error_event("t", "query-failed", "x"), protocol.progress_event("t", {"type": "final"}),
		):
			line = protocol.encode_event(event)
			self.assertTrue(line.endswith("\n"))
			self.assertEqual(line.count("\n"), 1)
			self.assertEqual(json.loads(line), event)

	def test_text_is_cut_into_chunks_that_join_back_to_the_text(self):
		text = "".join(chr(0x4e00 + index % 500) + ("\n" if index % 17 == 0 else "") for index in range(1000))

		chunks = list(protocol.text_chunks(text, 80))

		self.assertEqual("".join(chunks), text)
		self.assertTrue(all(0 < len(chunk) <= 80 for chunk in chunks))
		self.assertEqual(list(protocol.text_chunks("", 80)), [])
		self.assertEqual(list(protocol.text_chunks("short", 80)), ["short"])

	def test_worker_events_are_validated_on_the_way_back(self):
		good = [
			({"id": "t", "type": "progress", "kind": "tool", "name": "x", "arguments": "secret"}, {"id": "t", "type": "progress", "kind": "tool", "name": "x"}),
			({"id": "t", "type": "delta", "text": "abc"}, {"id": "t", "type": "delta", "text": "abc"}),
			({"id": "t", "type": "complete", "answer": "ok", "model": "m", "graph_version": "v", "reasoning": PRIVATE},
				{"id": "t", "type": "complete", "answer": "ok", "model": "m", "graph_version": "v"}),
			({"id": "t", "type": "complete", "capability": {"ready": True, "reason": None, "provenance": None, "extra": 1}},
				{"id": "t", "type": "complete", "capability": {"ready": True, "reason": None, "provenance": None}}),
			({"id": "t", "type": "complete"}, {"id": "t", "type": "complete"}),
			({"id": "t", "type": "error", "code": "provider-auth", "message": "no key"}, {"id": "t", "type": "error", "code": "provider-auth", "message": "no key"}),
			({"id": "t", "type": "error", "code": "made-up", "message": "x" * 900}, {"id": "t", "type": "error", "code": "query-failed", "message": "x" * 500}),
			({"id": None, "type": "ready", "protocol": 1}, {"id": None, "type": "ready", "protocol": 1}),
		]
		for raw, expected in good:
			with self.subTest(raw=raw["type"]):
				self.assertEqual(protocol.parse_event(json.dumps(raw)), expected)

	def test_malformed_worker_events_are_ignored(self):
		for bad in (
			"not json", "[1]", "{}", json.dumps({"id": "t", "type": "shell"}), json.dumps({"type": "delta", "text": "x"}),
			json.dumps({"id": "t", "type": "delta", "text": 5}), json.dumps({"id": "t", "type": "progress", "kind": "reasoning"}),
			json.dumps({"id": "bad id", "type": "delta", "text": "x"}), json.dumps({"id": "t", "type": "complete", "answer": 5}),
			json.dumps({"id": "t", "type": "complete", "answer": "x" * (protocol.MAX_ANSWER_BYTES + 1), "model": "m", "graph_version": "v"}),
			json.dumps({"id": "t", "type": "complete", "capability": "ready"}), "\xff",
		):
			with self.subTest(line=bad[:30]):
				self.assertIsNone(protocol.parse_event(bad))
		self.assertIsNone(protocol.parse_event(b"\xff\xfe"))

	def test_errors_are_described_without_their_original_text(self):
		def raised(name, message):
			return type(name, (Exception,), {})(message)

		cases = [
			(raised("AuthenticationError", "Incorrect API key provided: " + SECRET), "provider-auth"),
			(raised("PermissionDeniedError", SECRET), "provider-auth"),
			(raised("OpenAIError", "The api_key client option must be set " + SECRET), "provider-auth"),
			(raised("RateLimitError", "429 " + SECRET), "provider-rate-limit"),
			(raised("APIConnectionError", "Connection error " + SECRET), "provider-unreachable"),
			(raised("APITimeoutError", "timed out"), "provider-unreachable"),
			(raised("NotFoundError", "model x does not exist " + SECRET), "model-unavailable"),
			(RuntimeError("Traceback /home/user/secret/path " + SECRET), "query-failed"),
			(KeyError(SECRET), "query-failed"),
			(ValueError("anything " + SECRET), "invalid-request"),
			(MemoryError(), "query-failed"),
		]
		for error, code in cases:
			with self.subTest(error=type(error).__name__):
				got, message = protocol.public_error(error)
				self.assertEqual(got, code)
				self.assertNotIn(SECRET, message)
				self.assertNotIn("/home/user", message)
				self.assertLessEqual(len(message), 300)

	def test_a_query_that_is_not_ready_keeps_its_own_safe_message(self):
		code, message = protocol.public_error(QueryConfigurationError("the scene graph has no sentence embeddings: run Postprocess first"))

		self.assertEqual(code, "query-not-ready")
		self.assertIn("Postprocess", message)


class RedactTest(unittest.TestCase):
	def test_keys_and_credentials_are_masked(self):
		cases = [
			"Incorrect API key provided: sk-proj-abcDEF123456_ghiJKL. Find it at https://x",
			"key sk-ant-api03-abcdefghijklmnop failed",
			"Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789",
			'{"api_key": "plain-secret-value", "other": 1}',
			"OPENAI_API_KEY=plain-secret-value other=1",
			"token='plain-secret-value'",
			"password: hunter2hunter2",
			"glued to a word: LEAKsk-proj-abcdefghijklmnopqrstuvwxyz0123",
		]
		for line in cases:
			with self.subTest(line=line[:30]):
				masked = protocol.redact(line)
				for leaked in ("abcDEF123456", "abcdefghijklmnop", "abcdefghijklmnopqrstuvwxyz", "plain-secret-value", "hunter2hunter2"):
					self.assertNotIn(leaked, masked)
				self.assertIn("***", masked)

	def test_the_exact_secret_values_of_the_environment_are_masked_whatever_they_look_like(self):
		masked = protocol.redact("failed with weird-key-value-42 in the header", secrets=("weird-key-value-42", ""))

		self.assertEqual(masked, "failed with *** in the header")

	def test_ordinary_text_is_left_alone(self):
		for line in ("Iteration 3/15", "api key missing for the provider", "the task is done", "/opt/daaam_ws/src/daaam/scripts", "sk-"):
			self.assertEqual(protocol.redact(line), line)


class FakeAnswer:
	def __init__(self, answer, reasoning=PRIVATE):
		self.answer, self.reasoning = answer, reasoning


class FakeAgent:
	"""What SceneUnderstandingAgent.answer_query does for the worker: progress hooks, then a structured answer."""

	def __init__(self, answer="Beside the desk.", fail=None, fail_first_only=False):
		self.answer, self.fail, self.fail_first_only = answer, fail, fail_first_only
		self.calls = []

	def answer_query(self, response_format, query, image=None, *, conversation=None, context=None, progress=None, **kwargs):
		self.calls.append(SimpleNamespace(response_format=response_format, query=query, conversation=conversation, context=context))
		if self.fail is not None and (not self.fail_first_only or len(self.calls) == 1):
			raise self.fail
		progress({"type": "iteration", "number": 1, "maximum": 3})
		progress({"type": "tool", "name": "get_matching_subjects", "arguments": {"query": "chair"}, "result": SECRET, "reasoning": PRIVATE})
		progress({"type": "iteration", "number": 2, "maximum": 3})
		progress({"type": "final"})
		return FakeAnswer(self.answer), {"iterations": [{"response": PRIVATE}]}, [SECRET]


class FakeCache:
	def __init__(self, agent=None):
		self.agent = agent or FakeAgent()
		self.gets, self.invalidated = [], []

	def get(self, result_key, graph_path, graph_version, dataset, model_name, background_objects=None):
		self.gets.append(SimpleNamespace(result_key=result_key, path=Path(graph_path), version=graph_version, dataset=dataset,
			model=model_name, background=background_objects))
		return self.agent

	def invalidate(self, result_key):
		self.invalidated.append(result_key)


class WorkerTestCase(unittest.TestCase):
	def setUp(self):
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.repo = Path(self.directory.name)
		cache = self.repo / ".replay-web" / "cache" / ("b" * 64)
		cache.mkdir(parents=True)
		self.graph = cache / (DIGEST + ".json")
		self.graph.write_text(json.dumps(graph_document()), encoding="utf-8")
		self.background = cache / (DIGEST + ".background.yaml")
		self.background.write_text("objects: []\n", encoding="utf-8")
		self.cache = FakeCache()
		self.environ = {"OPENAI_API_KEY": SECRET}
		self.events = []

	def make_worker(self, **options):
		options.setdefault("cache", self.cache)
		options.setdefault("environ", self.environ)
		options.setdefault("response_format", "TEXT-RESPONSE")
		options.setdefault("models", ("gpt-5-mini", "claude-sonnet-4-5"))
		return agent_worker.Worker(self.repo, **options)

	def run_one(self, request, worker=None):
		worker = worker or self.make_worker()
		worker.handle(request, self.events.append)
		return worker

	def lines(self):
		return [protocol.encode_event(event) for event in self.events]


class WorkerAskTest(WorkerTestCase):
	def test_a_question_streams_progress_then_the_answer_in_chunks(self):
		self.cache.agent = FakeAgent("The chair is beside the desk, " * 12)
		request = parse_request(ask_request(conversation=[{"role": "user", "content": "Find the red chair."}], context={"replay_time": 12.5}))

		self.run_one(request)

		types = [(event["type"], event.get("kind")) for event in self.events]
		self.assertEqual(types[:6], [("progress", "loading"), ("progress", "iteration"), ("progress", "tool"), ("progress", "iteration"), ("progress", "final"), ("delta", None)])
		self.assertEqual(types[-1], ("complete", None))
		self.assertTrue(all(kind == "delta" for kind, _ in types[5:-1]))
		self.assertTrue(all(event["id"] == "turn-1" for event in self.events))
		answer = "".join(event["text"] for event in self.events if event["type"] == "delta")
		self.assertEqual(answer, "The chair is beside the desk, " * 12)
		complete = self.events[-1]
		self.assertEqual(complete, {"id": "turn-1", "type": "complete", "answer": answer, "model": "gpt-5-mini", "graph_version": "2-" + "c" * 20})
		self.assertTrue(all(len(event["text"]) <= 80 for event in self.events if event["type"] == "delta"))

	def test_the_agent_gets_the_conversation_the_context_and_the_cached_graph(self):
		request = parse_request(ask_request(conversation=[{"role": "user", "content": "Find the red chair."}], context={"replay_time": 12.5}))

		self.run_one(request)

		call = self.cache.agent.calls[0]
		self.assertEqual(call.response_format, "TEXT-RESPONSE")
		self.assertEqual(call.query, "Where is the chair?")
		self.assertEqual(list(call.conversation), [{"role": "user", "content": "Find the red chair."}])
		self.assertEqual(call.context, {"replay_time": 12.5})
		get = self.cache.gets[0]
		self.assertEqual((get.result_key, get.version, get.model), ("b" * 64, "2-" + "c" * 20, "gpt-5-mini"))
		self.assertEqual(get.path.resolve(), self.graph.resolve())
		self.assertEqual(get.dataset.kind, "tum")
		self.assertIsNone(get.background)

	def test_a_staged_background_file_is_passed_on(self):
		request = parse_request(ask_request(dataset={"kind": "coda", "sequence": "0", "time_origin": 1673884185.589118}, background_path=BACKGROUND_PATH))

		self.run_one(request)

		self.assertEqual(self.cache.gets[0].background.resolve(), self.background.resolve())

	def test_nothing_private_reaches_the_protocol_stream(self):
		self.run_one(parse_request(ask_request()))

		everything = "".join(self.lines())
		for private in (PRIVATE, SECRET, "query", "chair", "iterations"):
			self.assertNotIn(private, everything)

	def test_the_answer_is_clipped_to_the_stored_message_limit(self):
		self.cache.agent = FakeAgent("x" * (protocol.MAX_ANSWER_BYTES + 5000))

		self.run_one(parse_request(ask_request()))

		self.assertEqual(len(self.events[-1]["answer"].encode("utf-8")), protocol.MAX_ANSWER_BYTES)

	def test_a_missing_credential_is_reported_before_any_model_is_loaded(self):
		for model, environ in (("gpt-5-mini", {}), ("claude-sonnet-4-5", {"OPENAI_API_KEY": SECRET}), ("gpt-5-mini", {"OPENAI_API_KEY": ""})):
			with self.subTest(model=model):
				self.events.clear()
				self.run_one(parse_request(ask_request(model=model)), self.make_worker(environ=environ))

				self.assertEqual([event["type"] for event in self.events], ["error"])
				self.assertEqual(self.events[0]["code"], "no-credentials")
				self.assertEqual(self.cache.gets, [])
				self.assertNotIn(SECRET, "".join(self.lines()))

	def test_the_provider_follows_the_model_name(self):
		worker = self.make_worker(environ={"ANTHROPIC_API_KEY": SECRET})

		worker.handle(parse_request(ask_request(model="claude-sonnet-4-5")), self.events.append)

		self.assertEqual(self.events[-1]["type"], "complete")
		self.assertEqual(self.cache.gets[0].model, "claude-sonnet-4-5")

	def test_a_model_outside_the_workers_allowlist_is_refused(self):
		worker = self.make_worker(models=("gpt-5-mini",))

		worker.handle_line(json.dumps(ask_request(model="claude-sonnet-4-5")), self.events.append)

		self.assertEqual([(event["type"], event["code"]) for event in self.events], [("error", "invalid-request")])
		self.assertEqual(self.cache.gets, [])

	def test_a_failing_question_is_an_error_event_without_the_original_text(self):
		self.cache.agent = FakeAgent(fail=RuntimeError("provider said: " + SECRET + " at /home/user/x"))

		self.run_one(parse_request(ask_request()))

		self.assertEqual(self.events[-1]["type"], "error")
		self.assertEqual(self.events[-1]["code"], "query-failed")
		self.assertNotIn(SECRET, "".join(self.lines()))
		self.assertNotIn("/home/user", "".join(self.lines()))
		self.assertNotIn("complete", [event["type"] for event in self.events])

	def test_the_worker_log_never_holds_a_key(self):
		self.cache.agent = FakeAgent(fail=RuntimeError("provider said: " + SECRET + " and " + self.environ["OPENAI_API_KEY"]))
		log = []

		self.run_one(parse_request(ask_request()), self.make_worker(log=log.append))

		self.assertTrue(log)
		self.assertNotIn(SECRET, "".join(log))
		self.assertIn("RuntimeError", "".join(log))

	def test_a_graph_that_is_not_ready_is_reported_with_its_reason(self):
		class Refusing(FakeCache):
			def get(self, *args, **kwargs):
				raise QueryConfigurationError("the scene graph has no sentence embeddings: run Postprocess first")

		self.run_one(parse_request(ask_request()), self.make_worker(cache=Refusing()))

		self.assertEqual((self.events[-1]["code"], "Postprocess" in self.events[-1]["message"]), ("query-not-ready", True))


class WorkerFilesTest(WorkerTestCase):
	def test_a_snapshot_that_is_gone_is_reported(self):
		self.graph.unlink()

		self.run_one(parse_request(ask_request()))

		self.assertEqual([(event["type"], event.get("code")) for event in self.events][-1], ("error", "graph-missing"))
		self.assertEqual(self.cache.gets, [])

	def test_a_link_out_of_the_cache_is_not_followed(self):
		outside = self.repo / "elsewhere.json"
		outside.write_text(json.dumps(graph_document()), encoding="utf-8")
		self.graph.unlink()
		self.graph.symlink_to(outside)

		self.run_one(parse_request(ask_request()))

		self.assertEqual(self.events[-1]["code"], "graph-missing")
		self.assertEqual(self.cache.gets, [])

	def test_a_directory_is_not_a_snapshot(self):
		self.graph.unlink()
		self.graph.mkdir()

		self.run_one(parse_request(ask_request()))

		self.assertEqual(self.events[-1]["code"], "graph-missing")


class WorkerInspectTest(WorkerTestCase):
	def test_inspect_reports_the_capability_without_building_an_agent(self):
		document = graph_document()
		document["metadata"] = {"embedding_provenance": {
			"schema_version": 1,
			"sentence": {"model_name": "sentence-transformers/sentence-t5-xl", "backend": "sentence_transformers", "dim": 768, "pretrained": None},
			"clip": None,
		}}
		self.graph.write_text(json.dumps(document), encoding="utf-8")

		self.run_one(parse_request(without(ask_request(action="inspect"), "model", "question", "conversation", "context")), self.make_worker(environ={}))

		self.assertEqual([event["type"] for event in self.events], ["complete"])
		capability = self.events[0]["capability"]
		self.assertEqual((capability["ready"], capability["reason"]), (True, None))
		self.assertEqual(capability["provenance"]["sentence"]["model_name"], "sentence-transformers/sentence-t5-xl")
		self.assertEqual(self.cache.gets, [], "no Agent, no provider client")

	def test_a_graph_that_cannot_answer_says_why(self):
		self.graph.write_text(json.dumps(graph_document()), encoding="utf-8")   # provenance without embedding details

		self.run_one(parse_request(without(ask_request(action="inspect"), "model", "question", "conversation", "context")))

		capability = self.events[0]["capability"]
		self.assertIs(capability["ready"], False)
		self.assertTrue(capability["reason"])


class WorkerInvalidateTest(WorkerTestCase):
	def test_invalidate_drops_the_cached_result_and_acknowledges(self):
		self.run_one(parse_request({"id": "inv-1", "action": "invalidate", "result_key": "b" * 64}))

		self.assertEqual(self.cache.invalidated, ["b" * 64])
		self.assertEqual(self.events, [{"id": "inv-1", "type": "complete"}])


class WorkerLoopTest(WorkerTestCase):
	def serve(self, lines, **options):
		source = queue.Queue()
		for line in lines:
			source.put(line)
		source.put(None)
		worker = self.make_worker(**options)
		code = worker.serve(source, self.events.append)
		return worker, code

	def test_requests_are_answered_in_order_and_end_of_input_ends_the_worker(self):
		lines = [json.dumps(ask_request(id="t1")), json.dumps(ask_request(id="t2", question="And next to it?"))]

		_, code = self.serve(lines)

		self.assertEqual(code, 0)
		complete = [event["id"] for event in self.events if event["type"] == "complete"]
		self.assertEqual(complete, ["t1", "t2"])
		self.assertEqual([call.query for call in self.cache.agent.calls], ["Where is the chair?", "And next to it?"])

	def test_the_worker_announces_itself_first(self):
		self.serve([])

		self.assertEqual(self.events, [{"id": None, "type": "ready", "protocol": protocol.PROTOCOL_VERSION}])

	def test_a_bad_request_does_not_stop_the_worker(self):
		lines = ["not json", json.dumps(ask_request(id="t1", question="")), json.dumps(ask_request(id="t2")), "[1]"]

		_, code = self.serve(lines)

		self.assertEqual(code, 0)
		results = [(event["id"], event["type"], event.get("code")) for event in self.events if event["type"] in ("error", "complete")]
		self.assertEqual(results, [(None, "error", "invalid-request"), ("t1", "error", "invalid-request"), ("t2", "complete", None), (None, "error", "invalid-request")])

	def test_an_error_in_one_question_does_not_stop_the_next(self):
		self.cache.agent = FakeAgent(fail=RuntimeError("boom"), fail_first_only=True)

		_, code = self.serve([json.dumps(ask_request(id="t1")), json.dumps(ask_request(id="t2"))])

		self.assertEqual(code, 0)
		results = [(event["id"], event["type"]) for event in self.events if event["type"] in ("error", "complete")]
		self.assertEqual(results, [("t1", "error"), ("t2", "complete")])

	def test_an_oversized_line_is_refused_not_parsed(self):
		_, code = self.serve([b"x" * (protocol.MAX_LINE_BYTES + 10), json.dumps(ask_request(id="t2"))])

		self.assertEqual(code, 0)
		self.assertEqual([(event["type"], event.get("code")) for event in self.events if event["type"] != "progress" and event["type"] != "delta"],
			[("ready", None), ("error", "invalid-request"), ("complete", None)])

	def test_an_idle_worker_exits_so_it_frees_the_models(self):
		never = queue.Queue()
		worker = self.make_worker()
		finished = []

		thread = threading.Thread(target=lambda: finished.append(worker.serve(never, self.events.append, idle_seconds=0.2)))
		thread.start()
		thread.join(5)

		self.assertEqual(finished, [0])
		self.assertFalse(thread.is_alive())

	def test_the_idle_clock_restarts_with_every_request(self):
		source = queue.Queue()
		worker = self.make_worker()
		finished = []
		thread = threading.Thread(target=lambda: finished.append(worker.serve(source, self.events.append, idle_seconds=1.2)))
		thread.start()
		for number in range(3):
			threading.Event().wait(0.5)
			source.put(json.dumps(ask_request(id="t%d" % number)))
		threading.Event().wait(0.1)
		self.assertTrue(thread.is_alive(), "requests kept the worker busy for longer than the idle time")
		thread.join(5)

		self.assertEqual(finished, [0])
		self.assertEqual([event["id"] for event in self.events if event["type"] == "complete"], ["t0", "t1", "t2"])


if __name__ == "__main__":
	unittest.main()
