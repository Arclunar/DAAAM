"""Query setup of the replay chat: dataset context, graph loading, readiness and the Agent cache (no models, no network)."""
import contextlib
import dataclasses
import datetime
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts.replay_web import query
from scripts.replay_web.query import (
	AgentCache, DatasetContext, Encoder, HandlerPool, QueryConfigurationError, QueryProvenance, query_capability,
)
from tests.replay_fixtures import graph_document

try:
	import numpy as np
	import spark_dsg as sdsg
except ImportError:   # the host has no scene graph library: only the container runs these tests
	np = sdsg = None

CODA_ORIGIN = 1673884185.589118
SENTENCE = {"model_name": "sentence-transformers/sentence-t5-xl", "backend": "sentence_transformers", "dim": 768, "pretrained": None}
CLIP = {"model_name": "ViT-L-14", "backend": "openclip", "dim": 768, "pretrained": "openai"}
PROVENANCE = {"schema_version": 1, "sentence": SENTENCE, "clip": CLIP}
TUM = DatasetContext("tum", "fr1_desk", 0.0, "TRAVERSABILITY")
CODA = DatasetContext("coda", "0", CODA_ORIGIN, "TRAVERSABILITY")


def write_json(path, document):
	path = Path(path)
	path.write_text(json.dumps(document), encoding="utf-8")
	return path


def queryable_document(provenance=PROVENANCE, embedded=True):
	document = graph_document()
	document["metadata"] = {"embedding_provenance": provenance} if provenance is not None else {}
	if not embedded:
		for node in document["nodes"]:
			node["attributes"].get("metadata", {}).pop("sentence_embedding_feature", None)
	return document


class DatasetContextTest(unittest.TestCase):
	def test_the_context_comes_from_the_manifest_dataset(self):
		tum = DatasetContext.from_manifest({"kind": "tum", "sequence": "fr1_desk", "time_origin": 0.0, "fps": 15.0})
		coda = DatasetContext.from_manifest({"kind": "coda", "sequence": "0", "time_origin": CODA_ORIGIN, "fps": 10.0})

		self.assertEqual(tum, DatasetContext("tum", "fr1_desk", 0.0, "TRAVERSABILITY"))
		self.assertEqual(coda, DatasetContext("coda", "0", CODA_ORIGIN, "TRAVERSABILITY"))

	def test_the_context_is_immutable_and_usable_as_a_key(self):
		with self.assertRaises(dataclasses.FrozenInstanceError):
			TUM.kind = "coda"
		self.assertEqual({TUM: 1}[DatasetContext("tum", "fr1_desk", 0.0, "TRAVERSABILITY")], 1)
		self.assertNotEqual(TUM, DatasetContext("tum", "fr1_room", 0.0, "TRAVERSABILITY"))

	def test_unsupported_or_inconsistent_datasets_are_refused(self):
		bad = [
			{"kind": "kitti", "sequence": "0", "time_origin": 0.0},
			{"kind": "tum", "sequence": "", "time_origin": 0.0},
			{"kind": "tum", "sequence": 7, "time_origin": 0.0},
			{"kind": "tum", "sequence": "fr1_desk", "time_origin": 12.0},   # a TUM run is already relative to its start
			{"kind": "coda", "sequence": "0", "time_origin": float("nan")},
			{"kind": "coda", "sequence": "0", "time_origin": "1673884185.5"},
			{"kind": "coda", "sequence": "0", "time_origin": True},
			{"kind": "coda", "sequence": "0"},
			{"sequence": "0", "time_origin": 0.0},
		]
		for dataset in bad:
			with self.subTest(dataset=dataset):
				with self.assertRaises(QueryConfigurationError):
					DatasetContext.from_manifest(dataset)

	def test_the_errors_are_value_errors(self):
		self.assertTrue(issubclass(QueryConfigurationError, ValueError))


class QueryCapabilityTest(unittest.TestCase):
	def setUp(self):
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.root = Path(self.directory.name)

	def capability(self, document):
		return query_capability(write_json(self.root / "graph.json", document))

	def test_a_graph_with_sentence_provenance_and_embedded_objects_is_ready(self):
		capability = self.capability(queryable_document())

		self.assertTrue(capability.ready)
		self.assertIsNone(capability.reason)
		self.assertEqual(capability.provenance, QueryProvenance(Encoder(**SENTENCE), Encoder(**CLIP)))
		self.assertEqual(
			capability.to_json(),
			{"ready": True, "reason": None, "provenance": {"sentence": SENTENCE, "clip": CLIP}},
		)

	def test_clip_is_optional(self):
		capability = self.capability(queryable_document(dict(PROVENANCE, clip=None)))

		self.assertTrue(capability.ready)
		self.assertIsNone(capability.provenance.clip)

	def test_a_raw_mapping_graph_asks_for_postprocess(self):
		raw_pipeline_graph = queryable_document(dict(PROVENANCE, sentence=None), embedded=False)   # the CLIP is stamped, the sentence encoder is not
		for document in (raw_pipeline_graph, queryable_document(None, embedded=False)):
			with self.subTest(metadata=document["metadata"]):
				capability = self.capability(document)

				self.assertFalse(capability.ready)
				self.assertIn("Postprocess", capability.reason)
				self.assertIsNone(capability.provenance)

	def test_provenance_without_embedded_objects_is_not_ready(self):
		capability = self.capability(queryable_document(embedded=False))

		self.assertFalse(capability.ready)
		self.assertIn("embedding", capability.reason)
		self.assertEqual(capability.provenance.sentence.model_name, SENTENCE["model_name"], "the provenance is still reported")

	def test_malformed_provenance_is_not_ready(self):
		bad = [
			"sentence-t5",
			dict(PROVENANCE, schema_version=2),
			dict(PROVENANCE, schema_version="1"),
			dict(PROVENANCE, sentence={"model_name": "m"}),
			dict(PROVENANCE, sentence=dict(SENTENCE, dim="768")),
			dict(PROVENANCE, sentence=dict(SENTENCE, model_name="")),
			dict(PROVENANCE, clip=dict(CLIP, backend=None)),
		]
		for provenance in bad:
			with self.subTest(provenance=provenance):
				capability = self.capability(queryable_document(provenance))

				self.assertFalse(capability.ready)
				self.assertTrue(capability.reason)

	def test_an_unreadable_file_is_not_ready_and_the_reason_names_no_host_path(self):
		missing = query_capability(self.root / "nothing.json")
		broken = self.root / "broken.json"
		broken.write_text("{not json", encoding="utf-8")
		not_a_graph = write_json(self.root / "list.json", [1, 2, 3])

		for capability in (missing, query_capability(broken), query_capability(not_a_graph)):
			self.assertFalse(capability.ready)
			self.assertTrue(capability.reason)
			self.assertNotIn(str(self.root), capability.reason)
			self.assertIsNone(capability.provenance)


class ConfigTest(unittest.TestCase):
	@unittest.skipIf(sdsg is None, "the Agent configuration is only importable in the daaam-ws container")
	def test_the_agent_gets_exactly_the_four_read_only_tools_and_the_encoders_of_the_graph(self):
		provenance = QueryProvenance(Encoder(**SENTENCE), Encoder(**dict(CLIP, model_name="ViT-B-32", backend="pe")))

		config = query.agent_config("gpt-5-mini", provenance)

		self.assertEqual(config.model_name, "gpt-5-mini")
		self.assertEqual(config.available_tools, [
			"get_matching_subjects", "get_objects_in_radius", "get_region_information", "get_agent_trajectory_information",
		])
		self.assertEqual(query.QUERY_TOOLS, tuple(config.available_tools))
		self.assertEqual(config.tool_config.sentence_embedding_model_name, SENTENCE["model_name"])
		self.assertEqual((config.tool_config.clip_model_name, config.tool_config.clip_backend), ("ViT-B-32", "pe"))

	@unittest.skipIf(sdsg is None, "the Agent configuration is only importable in the daaam-ws container")
	def test_a_graph_without_clip_features_builds_no_clip_encoder(self):
		config = query.agent_config("gpt-5-mini", QueryProvenance(Encoder(**SENTENCE), None))

		self.assertIsNone(config.tool_config.clip_model_name)
		self.assertIsNone(config.tool_config.clip_backend)

	@unittest.skipIf(sdsg is None, "the Agent is only importable in the daaam-ws container")
	def test_build_agent_shares_the_pooled_encoders_and_loads_the_graph(self):
		provenance = QueryProvenance(Encoder(**SENTENCE), Encoder(**CLIP))
		made = []
		pool = HandlerPool(make_sentence=lambda encoder: made.append(("sentence", encoder)) or "SENTENCE-HANDLER",
			make_clip=lambda encoder: made.append(("clip", encoder)) or "CLIP-HANDLER")
		graph = object()
		with mock.patch("daaam.scene_understanding.services.SceneUnderstandingAgent") as agent_class:
			agent = query.build_agent("gpt-5-mini", provenance, graph, pool)

		self.assertIs(agent, agent_class.return_value)
		config, logger = agent_class.call_args[0][:2]
		self.assertEqual(config.model_name, "gpt-5-mini")
		self.assertEqual(agent_class.call_args[1]["clip_handler"], "CLIP-HANDLER")
		self.assertEqual(agent_class.call_args[1]["sentence_handler"], "SENTENCE-HANDLER")
		agent.update_scene_graph.assert_called_once_with(graph)
		self.assertEqual(made, [("sentence", provenance.sentence), ("clip", provenance.clip)])


class HandlerPoolTest(unittest.TestCase):
	def make_pool(self, limit=2):
		self.made = []
		return HandlerPool(
			make_sentence=lambda encoder: self.made.append(("sentence", encoder.model_name)) or "S-" + encoder.model_name,
			make_clip=lambda encoder: self.made.append(("clip", encoder.model_name)) or "C-" + encoder.model_name,
			limit=limit,
		)

	def test_an_encoder_is_built_once_and_shared(self):
		pool = self.make_pool()

		first = (pool.sentence(Encoder(**SENTENCE)), pool.clip(Encoder(**CLIP)))
		second = (pool.sentence(Encoder(**SENTENCE)), pool.clip(Encoder(**CLIP)))

		self.assertEqual(first, second)
		self.assertEqual(self.made, [("sentence", SENTENCE["model_name"]), ("clip", "ViT-L-14")])

	def test_no_clip_encoder_means_no_handler(self):
		pool = self.make_pool()

		self.assertIsNone(pool.clip(None))
		self.assertEqual(self.made, [])

	def test_encoders_differing_in_any_field_are_different_handlers(self):
		pool = self.make_pool(limit=4)

		pool.clip(Encoder(**CLIP))
		pool.clip(Encoder(**dict(CLIP, pretrained="laion2b")))
		pool.clip(Encoder(**dict(CLIP, backend="pe")))

		self.assertEqual(len(self.made), 3)

	def test_the_least_recently_used_encoder_is_dropped_beyond_the_limit(self):
		pool = self.make_pool(limit=2)
		a, b, c = (Encoder(**dict(CLIP, model_name=name)) for name in ("a", "b", "c"))

		pool.clip(a)
		pool.clip(b)
		pool.clip(a)   # a is now the most recently used
		pool.clip(c)   # b goes
		pool.clip(a)
		pool.clip(b)   # built again

		self.assertEqual([name for _, name in self.made], ["a", "b", "c", "b"])


class FakeGraph:
	def __init__(self, label, provenance=PROVENANCE):
		self.label = label
		self.metadata = SimpleNamespace(get=lambda: {"embedding_provenance": provenance} if provenance is not None else {})


class Builds:
	"""A loader and an Agent factory that record what the cache asks them to build."""

	def __init__(self, delay=0.0, provenance=PROVENANCE):
		self.delay = delay
		self.provenance = provenance
		self.loads = []
		self.agents = []
		self.fail_loads = 0

	def loader(self, graph_path, dataset, background_objects=None):
		time.sleep(self.delay)
		if self.fail_loads:
			self.fail_loads -= 1
			raise QueryConfigurationError("the scene graph cannot be loaded")
		self.loads.append((Path(graph_path).name, dataset.kind, background_objects))
		return FakeGraph(Path(graph_path).name, self.provenance)

	def factory(self, model_name, provenance, graph, handlers):
		agent = SimpleNamespace(model=model_name, provenance=provenance, graph=graph, handlers=handlers)
		self.agents.append(agent)
		return agent


class AgentCacheTest(unittest.TestCase):
	def setUp(self):
		self.builds = Builds()
		self.pool = HandlerPool(make_sentence=lambda encoder: object(), make_clip=lambda encoder: object())
		self.cache = AgentCache(max_graphs=2, loader=self.builds.loader, factory=self.builds.factory, handlers=self.pool)

	def get(self, key="a", version="v1", model="gpt-5-mini", dataset=TUM, graph="graph-a.json", **kwargs):
		return self.cache.get(key, Path("/cache") / graph, version, dataset, model, **kwargs)

	def test_the_same_key_reuses_the_agent_and_the_graph(self):
		first = self.get()
		second = self.get()

		self.assertIs(first, second)
		self.assertEqual(len(self.builds.loads), 1)
		self.assertEqual(len(self.builds.agents), 1)

	def test_the_agent_gets_the_provenance_stored_in_the_graph(self):
		agent = self.get()

		self.assertEqual(agent.provenance, QueryProvenance(Encoder(**SENTENCE), Encoder(**CLIP)))
		self.assertEqual(agent.graph.label, "graph-a.json")
		self.assertIs(agent.handlers, self.pool)

	def test_a_new_graph_version_rebuilds_the_agent_and_replaces_the_entry(self):
		first = self.get(version="v1", graph="v1.json")
		second = self.get(version="v2", graph="v2.json")

		self.assertIsNot(first, second)
		self.assertEqual([load[0] for load in self.builds.loads], ["v1.json", "v2.json"])
		self.assertEqual(len(self.cache), 1, "one entry per result: the old version is gone")
		self.get(version="v1", graph="v1.json")
		self.assertEqual(len(self.builds.loads), 3, "the replaced version is loaded again")

	def test_a_new_model_keeps_the_graph_but_builds_its_own_agent(self):
		gpt = self.get(model="gpt-5-mini")
		claude = self.get(model="claude-sonnet-4-5")

		self.assertIsNot(gpt, claude)
		self.assertIs(gpt.graph, claude.graph)
		self.assertEqual((gpt.model, claude.model), ("gpt-5-mini", "claude-sonnet-4-5"))
		self.assertEqual(len(self.builds.loads), 1)
		self.assertIs(self.get(model="gpt-5-mini"), gpt)
		self.assertEqual(len(self.builds.agents), 2)

	def test_the_dataset_is_part_of_the_identity(self):
		tum = self.get(dataset=TUM)
		coda = self.get(dataset=CODA)

		self.assertIsNot(tum, coda)
		self.assertEqual([load[1] for load in self.builds.loads], ["tum", "coda"])

	def test_the_least_recently_used_result_is_evicted(self):
		self.get("a", graph="a.json")
		self.get("b", graph="b.json")
		self.get("a", graph="a.json")   # a is now the most recently used
		self.get("c", graph="c.json")   # b goes

		self.assertEqual(len(self.cache), 2)
		self.get("a", graph="a.json")
		self.get("c", graph="c.json")
		self.assertEqual([load[0] for load in self.builds.loads], ["a.json", "b.json", "c.json"])
		self.get("b", graph="b.json")
		self.assertEqual([load[0] for load in self.builds.loads], ["a.json", "b.json", "c.json", "b.json"])

	def test_an_evicted_agent_stays_usable_for_a_question_that_is_running(self):
		running = self.get("a", graph="a.json")
		self.get("b", graph="b.json")
		self.get("c", graph="c.json")   # a is evicted while its question runs

		self.assertEqual(running.graph.label, "a.json")

	def test_invalidate_drops_only_that_result(self):
		self.get("a", graph="a.json")
		self.get("b", graph="b.json")

		self.cache.invalidate("a")

		self.assertEqual(len(self.cache), 1)
		self.get("b", graph="b.json")
		self.assertEqual(len(self.builds.loads), 2)
		self.get("a", graph="a.json")
		self.assertEqual([load[0] for load in self.builds.loads], ["a.json", "b.json", "a.json"])

	def test_invalidating_an_unknown_result_is_harmless(self):
		self.cache.invalidate("nothing")
		self.assertEqual(len(self.cache), 0)

	def test_the_background_objects_path_reaches_the_loader(self):
		self.get(background_objects=Path("/cache/background.yaml"))

		self.assertEqual(self.builds.loads, [("graph-a.json", "tum", Path("/cache/background.yaml"))])

	def test_a_graph_without_sentence_provenance_is_refused_and_not_cached(self):
		self.builds.provenance = dict(PROVENANCE, sentence=None)

		with self.assertRaises(QueryConfigurationError) as caught:
			self.get()

		self.assertIn("Postprocess", str(caught.exception))
		self.assertEqual(len(self.cache), 0)
		self.assertEqual(self.builds.agents, [])

	def test_a_failing_load_is_not_cached(self):
		self.builds.fail_loads = 1

		with self.assertRaises(QueryConfigurationError):
			self.get()
		agent = self.get()

		self.assertEqual(agent.graph.label, "graph-a.json")
		self.assertEqual(len(self.builds.loads), 1)

	def test_a_failing_factory_leaves_the_loaded_graph_for_the_next_try(self):
		attempts = []

		def flaky(model_name, provenance, graph, handlers):
			attempts.append(model_name)
			if len(attempts) == 1:
				raise RuntimeError("no GPU memory")
			return SimpleNamespace(model=model_name, graph=graph)

		cache = AgentCache(max_graphs=2, loader=self.builds.loader, factory=flaky, handlers=self.pool)

		with self.assertRaises(RuntimeError):
			cache.get("a", Path("/cache/a.json"), "v1", TUM, "gpt-5-mini")
		cache.get("a", Path("/cache/a.json"), "v1", TUM, "gpt-5-mini")

		self.assertEqual(len(self.builds.loads), 1, "the graph is not loaded twice")

	def test_concurrent_requests_for_one_result_build_it_once(self):
		builds = Builds(delay=0.15)
		cache = AgentCache(max_graphs=2, loader=builds.loader, factory=builds.factory, handlers=self.pool)
		results = []

		def ask():
			results.append(cache.get("a", Path("/cache/a.json"), "v1", TUM, "gpt-5-mini"))

		threads = [threading.Thread(target=ask) for _ in range(4)]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join(5)

		self.assertEqual(len(results), 4)
		self.assertTrue(all(agent is results[0] for agent in results))
		self.assertEqual(len(builds.loads), 1)

	def test_the_cache_needs_room_for_one_graph(self):
		for bad in (0, -1):
			with self.assertRaises(ValueError):
				AgentCache(max_graphs=bad)


@unittest.skipIf(sdsg is None, "scene graph loading is only available in the daaam-ws container")
class LoadQueryGraphTest(unittest.TestCase):
	def setUp(self):
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.root = Path(self.directory.name)
		stdout = contextlib.redirect_stdout(io.StringIO())   # the preprocessing reports what it dropped
		self.output = stdout.__enter__()
		self.addCleanup(stdout.__exit__, None, None, None)

	@staticmethod
	def node(index, first, last, description="a chair"):
		attributes = sdsg.ObjectNodeAttributes()
		attributes.position = np.array([float(index), 0.0, 1.0])
		attributes.semantic_label = 40 + index
		metadata = {"temporal_history": {"first_observed": first, "last_observed": last, "timestamps": [first, last]}}
		if description:
			metadata["description"] = description
		attributes.metadata.set(metadata)
		return attributes

	def write_graph(self, base, name="dsg.json", background_first=None):
		"""Objects O1 (described) and O2 (not), one background object, two agent poses; times are `base` plus seconds."""
		graph = sdsg.DynamicSceneGraph()
		graph.add_layer(2, 2, "BACKGROUND_OBJECTS")
		graph.add_layer(3, 2, "TRAVERSABILITY")
		graph.add_layer(2, ord("a"), "AGENTS")
		graph.add_node(sdsg.DsgLayers.OBJECTS, sdsg.NodeSymbol("O", 1), self.node(1, base + 10.0, base + 14.0))
		graph.add_node(sdsg.DsgLayers.OBJECTS, sdsg.NodeSymbol("O", 2), self.node(2, base + 10.0, base + 14.0, description=None))
		first = base + 5.0 if background_first is None else background_first
		graph.add_node("BACKGROUND_OBJECTS", sdsg.NodeSymbol("B", 1), self.node(3, first, first + 2.0))
		for index, seconds in enumerate((1.0, 3.0)):
			agent = sdsg.AgentNodeAttributes()
			agent.world_R_body = sdsg.Quaternion(1.0, 0.0, 0.0, 0.0)
			agent.external_key = index
			agent.position = np.array([float(index), 0.0, 0.0])
			agent.timestamp = datetime.timedelta(seconds=base + seconds)
			graph.add_node(2, sdsg.NodeSymbol("a", index), agent, ord("a"))
		path = self.root / name
		graph.save(str(path))
		return path

	@staticmethod
	def history(graph, symbol, layer=sdsg.DsgLayers.OBJECTS if sdsg is not None else None):
		for node in graph.get_layer(layer).nodes:
			if str(node.id) == symbol:
				return node.attributes.metadata.get()["temporal_history"]
		raise AssertionError("no node " + symbol)

	@staticmethod
	def agent_times(graph):
		return sorted(node.attributes.metadata.get()["timestamp"] for node in graph.get_layer(2, 97).nodes)

	def test_fixture_agent_poses_have_valid_identity_orientation_and_stable_keys(self):
		graph = sdsg.DynamicSceneGraph.load(str(self.write_graph(0.0)))
		for node in graph.get_layer(2, ord("a")).nodes:
			rotation = node.attributes.world_R_body
			np.testing.assert_allclose([rotation.w, rotation.x, rotation.y, rotation.z], [1.0, 0.0, 0.0, 0.0])
			self.assertEqual(node.attributes.external_key, node.id.category_id)

	def test_tum_keeps_its_relative_timestamps(self):
		path = self.write_graph(0.0)

		graph = query.load_query_graph(path, TUM)

		self.assertEqual(self.history(graph, "O(1)")["first_observed"], 10.0)
		self.assertEqual(self.history(graph, "O(1)")["last_observed"], 14.0)
		self.assertEqual(self.history(graph, "O(1)")["timestamps"], [10.0, 14.0])
		self.assertEqual(self.agent_times(graph), [1.0, 3.0], "the agent poses are seconds on the run's own clock")
		self.assertNotIn("WARNING", self.output.getvalue(), "an origin of 0 is not a suspicious Unix time")

	def test_coda_shifts_absolute_timestamps_by_the_manifest_origin(self):
		path = self.write_graph(CODA_ORIGIN)

		graph = query.load_query_graph(path, CODA)

		self.assertAlmostEqual(self.history(graph, "O(1)")["first_observed"], 10.0, places=4)
		self.assertAlmostEqual(self.history(graph, "O(1)")["last_observed"], 14.0, places=4)
		for got, expected in zip(self.agent_times(graph), (1.0, 3.0)):
			self.assertAlmostEqual(got, expected, places=4)

	def test_objects_without_a_description_are_dropped_for_both_datasets(self):
		for dataset, base in ((TUM, 0.0), (CODA, CODA_ORIGIN)):
			with self.subTest(kind=dataset.kind):
				graph = query.load_query_graph(self.write_graph(base), dataset)

				self.assertEqual([str(node.id) for node in graph.get_layer(sdsg.DsgLayers.OBJECTS).nodes], ["O(1)"])

	def test_coda_background_objects_are_enriched_from_the_optional_yaml(self):
		path = self.write_graph(CODA_ORIGIN, background_first=CODA_ORIGIN + 50.0)
		without = query.load_query_graph(path, CODA)
		(self.root / "background_objects.yaml").write_text(
			"objects:\n- semantic_id: 43\n  first_observed: %r\n  last_observed: %r\n  observations: 9\n" % (CODA_ORIGIN + 7.0, CODA_ORIGIN + 9.0),
			encoding="utf-8",
		)

		enriched = query.load_query_graph(path, CODA)

		self.assertAlmostEqual(self.history(without, "B(1)", "BACKGROUND_OBJECTS")["first_observed"], 50.0, places=4)
		self.assertAlmostEqual(self.history(enriched, "B(1)", "BACKGROUND_OBJECTS")["first_observed"], 7.0, places=4)

	def test_an_explicit_background_file_replaces_the_sibling_lookup(self):
		path = self.write_graph(CODA_ORIGIN, background_first=CODA_ORIGIN + 50.0)
		elsewhere = self.root / "staged"
		elsewhere.mkdir()
		(elsewhere / "background.yaml").write_text(
			"objects:\n- semantic_id: 43\n  first_observed: %r\n  last_observed: %r\n" % (CODA_ORIGIN + 8.0, CODA_ORIGIN + 9.0),
			encoding="utf-8",
		)

		graph = query.load_query_graph(path, CODA, background_objects=elsewhere / "background.yaml")

		self.assertAlmostEqual(self.history(graph, "B(1)", "BACKGROUND_OBJECTS")["first_observed"], 8.0, places=4)

	def test_tum_never_reads_a_background_yaml(self):
		path = self.write_graph(0.0, background_first=5.0)
		(self.root / "background_objects.yaml").write_text(
			"objects:\n- semantic_id: 43\n  first_observed: 99.0\n  last_observed: 100.0\n", encoding="utf-8",
		)

		graph = query.load_query_graph(path, TUM)

		self.assertEqual(self.history(graph, "B(1)", "BACKGROUND_OBJECTS")["first_observed"], 5.0)

	def test_a_graph_that_cannot_be_loaded_is_a_configuration_error_without_the_path(self):
		for path in (self.root / "missing.json", write_json(self.root / "broken.json", {"not": "a graph"})):
			with self.subTest(path=path.name):
				with self.assertRaises(QueryConfigurationError) as caught:
					query.load_query_graph(path, TUM)

				self.assertNotIn(str(self.root), str(caught.exception))


@unittest.skipIf(sdsg is None, "the demo CLI is only importable in the daaam-ws container")
class DemoQueryCommandTest(unittest.TestCase):
	"""The demo CLI keeps its CODa invocations and learns TUM."""

	@classmethod
	def setUpClass(cls):
		from click.testing import CliRunner
		from scripts import demo_query
		cls.demo_query, cls.runner = demo_query, CliRunner()

	def test_the_help_documents_both_datasets(self):
		result = self.runner.invoke(self.demo_query.main, ["--help"])

		self.assertEqual(result.exit_code, 0, result.output)
		for text in ("--dataset-kind", "--time-origin", "--seq-id", "tum", "coda", "clustered_dsg.json", "--seq-id 0"):
			self.assertIn(text, result.output)

	def test_seq_id_alone_still_means_a_coda_sequence(self):
		from daaam.utils.evaluation import START_TIMES

		context = self.demo_query.dataset_context(None, 0, None, None)

		self.assertEqual(context, DatasetContext("coda", "0", START_TIMES[0], "TRAVERSABILITY"))

	def test_tum_needs_no_sequence_id_and_starts_at_zero(self):
		self.assertEqual(self.demo_query.dataset_context("tum", None, None, "fr1_desk"), TUM)
		self.assertEqual(self.demo_query.dataset_context("tum", None, 0.0, "fr1_desk"), TUM)

	def test_an_explicit_time_origin_replaces_the_start_time_table(self):
		context = self.demo_query.dataset_context("coda", 0, CODA_ORIGIN + 1.0, None)

		self.assertEqual(context.time_origin, CODA_ORIGIN + 1.0)
		self.assertEqual(self.demo_query.dataset_context("coda", None, CODA_ORIGIN, "0"), CODA)

	def test_incomplete_or_contradicting_options_are_usage_errors(self):
		import click

		bad = [
			(None, None, None, None),   # no dataset at all
			("coda", None, None, None),   # no start time
			("coda", 99, None, None),   # unknown sequence
			("tum", None, 12.0, None),   # a TUM graph starts at 0
		]
		for arguments in bad:
			with self.subTest(arguments=arguments):
				with self.assertRaises(click.UsageError):
					self.demo_query.dataset_context(*arguments)


if __name__ == "__main__":
	unittest.main()
