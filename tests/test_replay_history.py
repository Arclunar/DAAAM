"""Persistent per-result chat history of the replay workspace (standard library only, Python 3.8)."""
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts.replay_web import history
from scripts.replay_web.history import ChatMessage, ConversationStore, GraphRef, HistoryError, history_key

MODEL = "gpt-5-mini"
RAW = GraphRef("dsg.json", "3-aaaaaaaaaaaaaaaaaaaa")
UPDATED = GraphRef("dsg_updated.json", "2-bbbbbbbbbbbbbbbbbbbb")
SUMMARIZED = GraphRef("clustered_dsg_with_summaries.json", "0-cccccccccccccccccccc")


class HistoryTestCase(unittest.TestCase):
	def setUp(self):
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.root = Path(self.directory.name) / "history"   # created by the first message, like .replay-web/history
		self.data = Path(self.directory.name) / "data"
		(self.data / "a").mkdir(parents=True)
		(self.data / "b").mkdir()

	def store(self, name="a"):
		return ConversationStore(self.root, self.data / name)

	def exchange(self, store, question, answer, graph=UPDATED):
		store.append_user(question, model=MODEL, graph=graph)
		store.append_assistant(answer, model=MODEL, graph=graph)


class HistoryKeyTest(HistoryTestCase):
	def test_the_key_is_the_sha256_of_the_canonical_path(self):
		expected = hashlib.sha256(os.fsencode(str((self.data / "a").resolve()))).hexdigest()

		self.assertEqual(history_key(self.data / "a"), expected)
		self.assertRegex(history_key(self.data / "a"), r"^[0-9a-f]{64}$")

	def test_equivalent_spellings_of_one_result_share_a_key(self):
		link = Path(self.directory.name) / "link"
		link.symlink_to(self.data / "a")

		keys = {history_key(path) for path in (
			self.data / "a", self.data / "a" / "", self.data / "." / "a", self.data / "b" / ".." / "a", link,
		)}

		self.assertEqual(len(keys), 1)

	def test_different_results_have_different_keys(self):
		self.assertNotEqual(history_key(self.data / "a"), history_key(self.data / "b"))


class PersistenceTest(HistoryTestCase):
	def test_messages_survive_a_new_store_and_stay_isolated_per_result(self):
		first, second = self.store("a"), self.store("b")

		first.append_user("Where is the chair?", model=MODEL, graph=UPDATED)
		first.append_assistant("Beside the desk.", model=MODEL, graph=UPDATED)

		again = self.store("a").messages()
		self.assertEqual([(m.role, m.content) for m in again], [("user", "Where is the chair?"), ("assistant", "Beside the desk.")])
		self.assertEqual(second.messages(), [])
		self.assertEqual(self.store("b").messages(), [])

	def test_a_row_records_the_model_the_graph_and_the_context(self):
		store = self.store()
		context = {"replay_time": 12.5, "selected_entity": "O7", "camera": {"position": [1, 2, 3]}}

		user = store.append_user("What is this?", model=MODEL, graph=UPDATED, context=dict(context, api_key="SECRET"))
		answer = store.append_assistant("A chair.", model=MODEL, graph=UPDATED)

		self.assertEqual((user.role, user.model, user.graph_name, user.graph_version), ("user", MODEL, "dsg_updated.json", UPDATED.version))
		self.assertEqual(user.context, context, "only the replay fields are kept")
		self.assertIsNone(answer.context)
		self.assertEqual(self.store().messages(), [user, answer])
		self.assertEqual(ChatMessage.from_json(user.to_json()), user)
		self.assertNotEqual(user.message_id, answer.message_id)
		self.assertGreater(user.created_at, 1e9)
		self.assertLessEqual(user.created_at, answer.created_at)

	def test_the_graph_can_be_given_as_an_artifact_version_or_a_mapping(self):
		store = self.store()

		by_object = store.append_user("a", model=MODEL, graph=SimpleNamespace(name="dsg.json", token="3-xyz", digest="ignored"))
		by_mapping = store.append_user("b", model=MODEL, graph={"name": "dsg.json", "version": "3-xyz"})

		self.assertEqual((by_object.graph_name, by_object.graph_version), ("dsg.json", "3-xyz"))
		self.assertEqual((by_mapping.graph_name, by_mapping.graph_version), ("dsg.json", "3-xyz"))
		for bad in (None, "dsg.json", {"name": "dsg.json"}, SimpleNamespace(name=3, token="x")):
			with self.assertRaises(HistoryError):
				store.append_user("c", model=MODEL, graph=bad)

	def test_a_nested_camera_description_survives_a_restart(self):
		matrix = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
		store = self.store()

		stored = store.append_user("q", model=MODEL, graph=RAW, context={"camera": {"position": [1, 2, 3], "matrix": matrix}})

		self.assertEqual(self.store().messages(), [stored])
		self.assertFalse((store.directory / "quarantine.jsonl").exists())

	def test_errors_and_boundaries_are_visible_rows_with_their_own_role(self):
		store = self.store()
		store.append_user("q", model=MODEL, graph=RAW)

		error = store.append_error("the model is unavailable", model=MODEL, graph=RAW)
		coded = store.append_error("no key", code="provider-auth")

		self.assertEqual([m.role for m in store.messages()], ["user", "error", "error"])
		self.assertEqual((error.model, error.graph_name, error.context), (MODEL, "dsg.json", None))
		self.assertEqual((coded.context, coded.model, coded.graph_name), ({"code": "provider-auth"}, None, None))
		self.assertEqual(self.store().messages()[-1], coded)
		for bad in ("", "Provider Auth", "x" * 80, 7):
			with self.assertRaises(HistoryError):
				store.append_error("x", code=bad)

	def test_nothing_is_created_for_a_result_nobody_asked_about(self):
		store = self.store()

		self.assertEqual(store.messages(), [])
		self.assertEqual(store.context_messages(), [])
		store.clear()

		self.assertFalse(self.root.exists())

	def test_the_files_are_private_to_the_user(self):
		store = self.store()
		store.append_user("q", model=MODEL, graph=RAW)

		for path, expected in ((store.directory, 0o700), (store.directory / "messages.jsonl", 0o600), (store.directory / "meta.json", 0o600)):
			self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected, path.name)

	def test_every_append_is_flushed_to_disk(self):
		store = self.store()

		with mock.patch.object(history.os, "fsync", wraps=os.fsync) as fsync:
			store.append_user("q", model=MODEL, graph=RAW)
			store.append_assistant("a", model=MODEL, graph=RAW)

		self.assertGreaterEqual(fsync.call_count, 2)
		lines = (store.directory / "messages.jsonl").read_text(encoding="utf-8").splitlines()
		self.assertEqual([json.loads(line)["role"] for line in lines], ["user", "assistant"])

	def test_concurrent_appends_keep_every_row_whole(self):
		store = self.store()

		def work(number):
			for index in range(10):
				store.append_user("question %d-%d " % (number, index) + "é" * 200, model=MODEL, graph=RAW)

		threads = [threading.Thread(target=work, args=(number,)) for number in range(6)]
		with mock.patch.object(history.os, "fsync"):
			for thread in threads:
				thread.start()
			for thread in threads:
				thread.join(10)

		messages = self.store().messages()
		self.assertEqual(len(messages), 60)
		self.assertEqual(len({m.message_id for m in messages}), 60)
		self.assertFalse((store.directory / "quarantine.jsonl").exists())

	def test_oversized_text_is_refused(self):
		store = self.store()
		limit = history.MAX_MESSAGE_BYTES

		store.append_user("x" * limit, model=MODEL, graph=RAW)   # exactly the limit is fine
		for text in ("x" * (limit + 1), "é" * (limit // 2 + 1)):
			with self.assertRaises(HistoryError):
				store.append_assistant(text, model=MODEL, graph=RAW)
		self.assertEqual(len(store.messages()), 1)
		self.assertEqual(len(history.clip_message("x" * (limit + 5))), limit)
		self.assertEqual(history.clip_message("short"), "short")
		self.assertLessEqual(len(history.clip_message("é" * limit).encode("utf-8")), limit)

	def test_the_context_is_normalised(self):
		self.assertIsNone(history.sanitize_context(None))
		self.assertIsNone(history.sanitize_context({"api_key": "SECRET", "prompt": "hidden"}))
		self.assertIsNone(history.sanitize_context("replay_time"))
		kept = history.sanitize_context({"replay_time": 3, "selected_entity": "O1", "other": 1, "camera": {"fov": float("nan")}})
		self.assertEqual(kept, {"replay_time": 3, "selected_entity": "O1"}, "a value that is not plain finite JSON is dropped")
		matrix = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
		self.assertEqual(history.sanitize_context({"camera": {"position": [1, 2, 3], "matrix": matrix}}), {"camera": {"position": [1, 2, 3], "matrix": matrix}})
		huge = history.sanitize_context({"selected_entity": "x" * 5000, "replay_time": 1.0})
		self.assertEqual(huge, {"replay_time": 1.0}, "a field over the size limit is dropped")


class MetadataTest(HistoryTestCase):
	def test_metadata_records_the_schema_the_path_and_the_creation_time(self):
		store = self.store()
		store.append_user("q", model=MODEL, graph=RAW)

		meta = json.loads((store.directory / "meta.json").read_text(encoding="utf-8"))

		self.assertEqual(sorted(meta), ["canonical_path", "created_at", "schema_version"])
		self.assertEqual(meta["schema_version"], 1)
		self.assertEqual(meta["canonical_path"], str((self.data / "a").resolve()))
		self.assertGreater(meta["created_at"], 1e9)
		self.assertEqual(store.directory, self.root / history_key(self.data / "a"))

	def test_no_credential_is_written_anywhere_in_the_history(self):
		secrets = {"OPENAI_API_KEY": "sk-test-SECRET-1", "ANTHROPIC_API_KEY": "sk-ant-SECRET-2"}
		with mock.patch.dict(os.environ, secrets):
			store = self.store()
			self.exchange(store, "q", "a")
			store.append_error("failed", model=MODEL, graph=UPDATED)
			store.context_messages()

		for path in store.directory.iterdir():
			text = path.read_text(encoding="utf-8")
			for secret in secrets.values():
				self.assertNotIn(secret, text)

	def test_a_missing_or_broken_meta_file_is_rewritten_and_the_messages_stay(self):
		store = self.store()
		self.exchange(store, "q", "a")
		meta = store.directory / "meta.json"

		for damage in (lambda: meta.unlink(), lambda: meta.write_text("{broken", encoding="utf-8")):
			damage()
			store.append_user("again", model=MODEL, graph=UPDATED)
			self.assertEqual(json.loads(meta.read_text(encoding="utf-8"))["schema_version"], 1)
		self.assertEqual(len(store.messages()), 4)

	def test_a_newer_schema_is_left_alone(self):
		store = self.store()
		store.append_user("q", model=MODEL, graph=RAW)
		meta = store.directory / "meta.json"
		meta.write_text(json.dumps({"schema_version": 99, "canonical_path": "x", "created_at": 1.0}), encoding="utf-8")

		with self.assertRaises(HistoryError):
			store.append_user("again", model=MODEL, graph=RAW)
		with self.assertRaises(HistoryError):
			store.messages()
		self.assertEqual(json.loads(meta.read_text(encoding="utf-8"))["schema_version"], 99)


class RepairTest(HistoryTestCase):
	def test_a_malformed_trailing_line_is_quarantined_and_earlier_turns_stay(self):
		store = self.store()
		self.exchange(store, "q1", "a1")
		path = store.directory / "messages.jsonl"
		with path.open("ab") as stream:
			stream.write(b'{"message_id":"half","role":"user","con')   # a crash in the middle of a write

		messages = self.store().messages()

		self.assertEqual([m.content for m in messages], ["q1", "a1"])
		quarantine = (store.directory / "quarantine.jsonl").read_text(encoding="utf-8").splitlines()
		self.assertEqual(len(quarantine), 1)
		self.assertIn('"message_id":"half"', json.loads(quarantine[0])["line"])
		self.assertTrue(path.read_bytes().endswith(b"\n"), "the file ends on a whole line again")
		self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)
		store.append_user("q2", model=MODEL, graph=UPDATED)
		self.assertEqual([m.content for m in self.store().messages()], ["q1", "a1", "q2"])

	def test_malformed_lines_in_the_middle_are_quarantined_too(self):
		store = self.store()
		store.append_user("q1", model=MODEL, graph=UPDATED)
		path = store.directory / "messages.jsonl"
		good = path.read_text(encoding="utf-8")
		bad = [
			"not json",
			json.dumps({"message_id": "x", "role": "system", "content": "c", "created_at": 1.0}),   # a role history cannot hold
			json.dumps({"message_id": "x", "role": "user", "content": 7, "created_at": 1.0}),
			json.dumps(["list"]),
			json.dumps({"message_id": "x"}),   # valid JSON with nothing a message needs
		]
		path.write_text(good + "\n".join(bad) + "\n" + good, encoding="utf-8")

		messages = self.store().messages()

		self.assertEqual([m.content for m in messages], ["q1", "q1"])
		self.assertEqual(len((store.directory / "quarantine.jsonl").read_text(encoding="utf-8").splitlines()), len(bad))

	def test_a_file_that_is_not_text_is_quarantined_line_by_line(self):
		store = self.store()
		store.append_user("q1", model=MODEL, graph=UPDATED)
		with (store.directory / "messages.jsonl").open("ab") as stream:
			stream.write(b"\xff\xfe\x00 not utf-8\n")

		self.assertEqual([m.content for m in self.store().messages()], ["q1"])
		self.assertTrue((store.directory / "quarantine.jsonl").exists())


class BoundaryTest(HistoryTestCase):
	def test_the_first_message_needs_no_boundary(self):
		store = self.store()

		self.assertIsNone(store.last_graph())
		self.assertIsNone(store.append_boundary(RAW))
		self.assertEqual(store.messages(), [])

	def test_a_changed_graph_adds_one_visible_boundary(self):
		store = self.store()
		self.exchange(store, "q1", "a1", RAW)

		self.assertIsNone(store.append_boundary(RAW), "the same version again")
		boundary = store.append_boundary(UPDATED)
		self.assertIsNone(store.append_boundary(UPDATED), "consecutive identical versions are not repeated")

		self.assertEqual(boundary.role, "boundary")
		self.assertEqual((boundary.graph_name, boundary.graph_version), ("dsg_updated.json", UPDATED.version))
		self.assertEqual(boundary.context, {"from": {"name": "dsg.json", "version": RAW.version}})
		self.assertIn("dsg_updated.json", boundary.content)
		self.assertEqual([m.role for m in store.messages()], ["user", "assistant", "boundary"])
		self.assertEqual(store.last_graph(), UPDATED)

	def test_the_same_file_with_new_content_is_a_new_version(self):
		store = self.store()
		self.exchange(store, "q1", "a1", RAW)

		boundary = store.append_boundary(GraphRef("dsg.json", "3-dddddddddddddddddddd"))

		self.assertIsNotNone(boundary)

	def test_the_last_graph_survives_a_restart(self):
		store = self.store()
		self.exchange(store, "q1", "a1", RAW)
		store.append_boundary(UPDATED)

		reopened = self.store()

		self.assertEqual(reopened.last_graph(), UPDATED)
		self.assertIsNone(reopened.append_boundary(UPDATED))
		self.assertIsNotNone(reopened.append_boundary(SUMMARIZED))

	def test_an_error_row_counts_as_a_graph_the_conversation_has_seen(self):
		store = self.store()
		store.append_user("q1", model=MODEL, graph=RAW)
		store.append_error("failed", model=MODEL, graph=RAW)

		self.assertIsNone(store.append_boundary(RAW))
		self.assertEqual(store.last_graph(), RAW)


class ClearTest(HistoryTestCase):
	def test_clearing_one_result_leaves_the_others(self):
		a, b = self.store("a"), self.store("b")
		self.exchange(a, "qa", "aa")
		self.exchange(b, "qb", "ab")

		a.clear()

		self.assertEqual(self.store("a").messages(), [])
		self.assertEqual([m.content for m in self.store("b").messages()], ["qb", "ab"])
		self.assertEqual([path.name for path in self.root.iterdir()], [history_key(self.data / "b")], "no trash is left behind")

	def test_a_cleared_conversation_can_start_again(self):
		store = self.store()
		self.exchange(store, "q1", "a1", RAW)
		store.clear()

		self.assertIsNone(store.last_graph())
		store.append_user("q2", model=MODEL, graph=UPDATED)
		self.assertEqual([m.content for m in self.store().messages()], ["q2"])
		meta = json.loads((store.directory / "meta.json").read_text(encoding="utf-8"))
		self.assertEqual(meta["canonical_path"], str((self.data / "a").resolve()))

	def test_clearing_never_follows_a_link_out_of_the_history_root(self):
		outside = Path(self.directory.name) / "outside"
		outside.mkdir()
		(outside / "precious.txt").write_text("keep", encoding="utf-8")
		store = self.store()
		self.root.mkdir(parents=True)
		store.directory.symlink_to(outside, target_is_directory=True)

		store.clear()

		self.assertEqual((outside / "precious.txt").read_text(encoding="utf-8"), "keep")
		self.assertFalse(os.path.lexists(str(store.directory)), "the link itself is gone, its target is not")

	def test_a_linked_history_directory_is_refused(self):
		outside = Path(self.directory.name) / "outside"
		outside.mkdir()
		store = self.store()
		self.root.mkdir(parents=True)
		store.directory.symlink_to(outside, target_is_directory=True)

		with self.assertRaises(HistoryError):
			store.append_user("q", model=MODEL, graph=RAW)
		with self.assertRaises(HistoryError):
			store.messages()
		self.assertEqual(list(outside.iterdir()), [])

	def test_stale_trash_is_removed_but_only_direct_children_of_the_root(self):
		self.root.mkdir(parents=True)
		stale = self.root / ".trash-abc-1"
		(stale / "inner").mkdir(parents=True)
		(stale / "inner" / "messages.jsonl").write_text("private", encoding="utf-8")
		keep = self.root / "not-trash"
		keep.mkdir()
		elsewhere = Path(self.directory.name) / "elsewhere"
		elsewhere.mkdir()
		(elsewhere / "file").write_text("keep", encoding="utf-8")
		(self.root / ".trash-link").symlink_to(elsewhere, target_is_directory=True)

		history.remove_stale_trash(self.root)

		self.assertFalse(stale.exists())
		self.assertTrue(keep.exists())
		self.assertTrue((elsewhere / "file").exists(), "a link named like trash is unlinked, never followed")
		self.assertFalse(os.path.lexists(str(self.root / ".trash-link")))
		history.remove_stale_trash(Path(self.directory.name) / "no-such-root")   # nothing to do


class ContextTest(HistoryTestCase):
	def test_a_short_conversation_is_sent_whole_without_boundaries_and_errors(self):
		store = self.store()
		self.exchange(store, "q1", "a1", RAW)
		store.append_boundary(UPDATED)
		store.append_user("q2", model=MODEL, graph=UPDATED)
		store.append_error("failed", model=MODEL, graph=UPDATED)
		self.exchange(store, "q3", "a3")

		self.assertEqual(store.context_messages(), [
			{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
			{"role": "user", "content": "q3"}, {"role": "assistant", "content": "a3"},
		], "q2 failed: it stays in the history but is not part of the conversation")
		self.assertNotIn("summary", json.loads((store.directory / "meta.json").read_text(encoding="utf-8")))

	def test_a_question_that_got_no_answer_is_not_part_of_the_conversation(self):
		store = self.store()
		self.exchange(store, "q1", "a1")
		store.append_user("failed once", model=MODEL, graph=UPDATED)
		store.append_error("provider down", model=MODEL, graph=UPDATED)
		store.append_user("failed twice", model=MODEL, graph=UPDATED)
		store.append_error("provider down", model=MODEL, graph=UPDATED)
		self.exchange(store, "the retry", "answered")
		store.append_user("still unanswered", model=MODEL, graph=UPDATED)   # asked last, no answer yet

		turns = store.context_messages()

		self.assertEqual([turn["content"] for turn in turns], ["q1", "a1", "the retry", "answered"])

	def test_a_boundary_between_a_question_and_its_answer_does_not_hide_the_answer(self):
		store = self.store()
		store.append_user("q1", model=MODEL, graph=RAW)
		store.append_boundary(UPDATED)
		store.append_assistant("a1", model=MODEL, graph=UPDATED)

		self.assertEqual([turn["content"] for turn in store.context_messages()], ["q1", "a1"])

	def test_the_window_holds_at_most_a_fixed_number_of_turns(self):
		store = self.store()
		with mock.patch.object(history.os, "fsync"):
			for number in range(400):
				self.exchange(store, "q%d" % number, "a%d" % number)

		turns = store.context_messages()

		self.assertLessEqual(len(turns), history.WINDOW_TURNS + 1, "the turns plus one summary")
		self.assertEqual(turns[-1], {"role": "assistant", "content": "a399"})
		self.assertTrue(turns[0]["content"].startswith("Earlier conversation summary:"))
		self.assertEqual(turns[1]["role"], "user")

	def long_history(self, count=200, graphs=(RAW, UPDATED, SUMMARIZED), question="Q%03d " + "where? " * 40, answer="A%03d " + "here. " * 120):
		store = self.store()
		with mock.patch.object(history.os, "fsync"):   # the durability of each append has its own test: this builds a long history fast
			for number in range(count):
				self.exchange(store, question % number, answer % number, graphs[number * len(graphs) // count])
		return store

	def test_a_long_conversation_becomes_one_bounded_summary_and_the_newest_turns(self):
		store = self.long_history()

		turns = store.context_messages()

		self.assertEqual(turns[0]["role"], "user")
		self.assertTrue(turns[0]["content"].startswith("Earlier conversation summary:\n"))
		summary_lines = turns[0]["content"].split("\n")[1:]
		self.assertTrue(summary_lines)
		self.assertTrue(all(len(line) <= 400 for line in summary_lines), max(len(line) for line in summary_lines))
		window = turns[1:]
		self.assertEqual(window[0]["role"], "user")
		self.assertEqual([turn["role"] for turn in window[-2:]], ["user", "assistant"])
		self.assertTrue(window[-2]["content"].startswith("Q199 "))
		self.assertTrue(window[-1]["content"].startswith("A199 "))
		self.assertLessEqual(sum(len(turn["content"]) for turn in window), 32000)
		self.assertLessEqual(sum(len(turn["content"]) for turn in turns), 48000)
		self.assertLessEqual(sum(len(turn["content"].encode("utf-8")) for turn in turns), 60000)
		self.assertEqual(turns, store.context_messages(), "deterministic")
		self.assertLessEqual({turn["content"] for turn in window}, {m.content for m in store.messages()}, "whole messages, not cuts")

	def test_summary_lines_keep_the_graph_each_answer_came_from_and_count_what_did_not_fit(self):
		store = self.long_history()
		turns = store.context_messages()
		older_exchanges = (len(store.messages()) - len(turns[1:])) // 2

		lines = turns[0]["content"].split("\n")[1:]
		exchanges = [line for line in lines if line.startswith("- [")]
		omitted = [line for line in lines if "omitted" in line]

		self.assertEqual(len(omitted), 1, "what did not fit is counted, once")
		self.assertEqual(len(exchanges) + int(re.search(r"\((\d+) earlier exchanges omitted\)", omitted[0]).group(1)), older_exchanges)
		self.assertTrue(all(re.match(r"^- \[(\S+\.json) \d-[0-9a-f]{20}\] Q: Q\d{3} .* A: A\d{3} ", line) for line in exchanges), exchanges[:2])
		attributed = {re.match(r"^- \[(\S+) ", line).group(1) for line in exchanges}
		self.assertEqual(attributed, {"dsg_updated.json", "clustered_dsg_with_summaries.json"}, "the newest summarized exchanges are kept")
		self.assertNotIn("Q000", turns[0]["content"], "the oldest exchange is the one that was left out")

	def test_the_summary_is_remembered_in_the_metadata(self):
		store = self.long_history()
		turns = store.context_messages()

		meta = json.loads((store.directory / "meta.json").read_text(encoding="utf-8"))

		self.assertEqual(meta["summary"]["text"], turns[0]["content"])
		older = len(store.messages()) - len([t for t in turns[1:]])
		self.assertEqual(meta["summary"]["messages"], older)
		self.assertEqual(meta["summary"]["through"], store.messages()[older - 1].message_id)
		self.assertEqual(sorted(meta), ["canonical_path", "created_at", "schema_version", "summary"])

	def test_a_conversation_in_a_wide_script_respects_the_byte_budget(self):
		store = self.long_history(count=40, question="问题%03d " + "它在哪里?" * 600, answer="回答%03d " + "在桌子旁边。" * 600)

		turns = store.context_messages()

		self.assertLessEqual(sum(len(turn["content"]) for turn in turns), 48000)
		self.assertLessEqual(sum(len(turn["content"].encode("utf-8")) for turn in turns), 60000)
		self.assertTrue(turns[-1]["content"].startswith("回答039 "))

	def test_the_budgets_hold_for_many_sizes(self):
		shapes = {
			"ascii": ("Q%03d " + "where? " * 40, "A%03d " + "here. " * 120),
			"wide": ("问题%03d " + "它在哪里?" * 50, "回答%03d " + "在桌子旁边。" * 100),
			"short": ("Q%03d?", "A%03d."),
		}
		for name, (question, answer) in shapes.items():
			store = self.long_history(count=60, question=question, answer=answer)
			store.clear()
			store = self.long_history(count=60, question=question, answer=answer)
			for max_chars, max_bytes in ((300, 400), (4000, 5000), (12000, 15000), (48000, 60000), (48000, 40000)):
				with self.subTest(shape=name, chars=max_chars, bytes=max_bytes):
					turns = store.context_messages(max_chars=max_chars, max_bytes=max_bytes)

					self.assertLessEqual(sum(len(turn["content"]) for turn in turns), max_chars)
					self.assertLessEqual(sum(len(turn["content"].encode("utf-8")) for turn in turns), max_bytes)
					self.assertTrue(turns and all(turn["content"] for turn in turns))
					self.assertEqual(turns[-1]["role"], "assistant")
					self.assertEqual(turns, store.context_messages(max_chars=max_chars, max_bytes=max_bytes))
					for line in (turns[0]["content"].split("\n") if turns[0]["content"].startswith(history.SUMMARY_HEADER) else []):
						self.assertLessEqual(len(line), 400)

	def test_the_budgets_can_be_lowered(self):
		store = self.long_history(count=20)

		turns = store.context_messages(max_chars=6000, max_bytes=8000)

		self.assertLessEqual(sum(len(turn["content"]) for turn in turns), 6000)
		self.assertLessEqual(sum(len(turn["content"].encode("utf-8")) for turn in turns), 8000)
		self.assertTrue(turns[-1]["content"].startswith("A019 "))

	def test_a_single_oversized_newest_message_is_clipped_not_dropped(self):
		store = self.store()
		store.append_user("short question", model=MODEL, graph=UPDATED)
		store.append_assistant("x" * 50000, model=MODEL, graph=UPDATED)

		turns = store.context_messages()

		self.assertEqual(turns[-1]["role"], "assistant")
		self.assertLessEqual(len(turns[-1]["content"]), 32000)
		self.assertLessEqual(sum(len(turn["content"]) for turn in turns), 48000)

	def test_the_context_turns_are_plain_role_content_pairs(self):
		store = self.long_history(count=60)

		for turn in store.context_messages():
			self.assertEqual(sorted(turn), ["content", "role"])
			self.assertIn(turn["role"], ("user", "assistant"))


if __name__ == "__main__":
	unittest.main()
