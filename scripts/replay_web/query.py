"""Query setup of the replay chat: how one result's scene graph becomes a queryable Agent.

The Agent worker and the demo CLI share this module. Importing it needs only the standard library; the scene graph and model
libraries are imported where they are used, inside the daaam-ws container.

The dataset kind, sequence and time origin come from the validated replay manifest and are never guessed from a folder name.
"""

import functools
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from scripts.replay_web.artifacts import ArtifactError, load_valid_graph

# The read-only scene tools of a question. Job methods are never part of an Agent's registry.
QUERY_TOOLS = ("get_matching_subjects", "get_objects_in_radius", "get_region_information", "get_agent_trajectory_information")
PLACES_LAYERS = {"tum": "PLACES", "coda": "TRAVERSABILITY"}
PROVENANCE_KEY = "embedding_provenance"
PROVENANCE_SCHEMA_VERSION = 1


class QueryConfigurationError(ValueError):
	"""A result that cannot be queried as configured. The message is safe to show: it names no host path."""


def _is_int(value: Any) -> bool:
	return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
	return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# ---------------------------------------------------------------------------------------------- dataset
@dataclass(frozen=True)
class DatasetContext:
	"""What the query side needs to know about a result's dataset.

	time_origin is the dataset time of t = 0 in seconds: a TUM run is already relative to its start (0), a CODa graph holds
	Unix times and is shifted by the sequence's first stamp.
	"""
	kind: str
	sequence: str
	time_origin: float
	places_layer: str

	def __post_init__(self) -> None:
		if not isinstance(self.kind, str) or self.kind not in PLACES_LAYERS:
			raise QueryConfigurationError("unsupported dataset kind: %s" % str(self.kind)[:40])
		if not isinstance(self.sequence, str) or not self.sequence:
			raise QueryConfigurationError("the dataset sequence must be a non-empty string")
		if not _is_number(self.time_origin):
			raise QueryConfigurationError("the dataset time origin must be a finite number of seconds")
		if self.kind == "tum" and self.time_origin != 0:
			raise QueryConfigurationError("a TUM scene graph is already relative to the run start: its time origin is 0")
		if self.places_layer != PLACES_LAYERS[self.kind]:
			raise QueryConfigurationError("the %s places layer is %s" % (self.kind, PLACES_LAYERS[self.kind]))

	@classmethod
	def from_manifest(cls, dataset: Any) -> "DatasetContext":
		"""The context of a replay manifest's `dataset` object."""
		if not isinstance(dataset, dict):
			raise QueryConfigurationError("the replay manifest has no dataset description")
		kind = dataset.get("kind")
		return cls(kind, dataset.get("sequence"), dataset.get("time_origin"), PLACES_LAYERS.get(kind, "") if isinstance(kind, str) else "")


# ---------------------------------------------------------------------------------------------- provenance
@dataclass(frozen=True)
class Encoder:
	"""One embedding model a scene graph was built with."""
	model_name: str
	backend: str
	dim: int
	pretrained: Optional[str] = None

	def to_json(self) -> Dict[str, Any]:
		return {"model_name": self.model_name, "backend": self.backend, "dim": self.dim, "pretrained": self.pretrained}


@dataclass(frozen=True)
class QueryProvenance:
	"""The encoders a question must be answered with. Sentence embeddings are required, CLIP ones are optional."""
	sentence: Encoder
	clip: Optional[Encoder] = None

	def to_json(self) -> Dict[str, Any]:
		return {"sentence": self.sentence.to_json(), "clip": self.clip.to_json() if self.clip is not None else None}


def _encoder(raw: Any, label: str) -> Encoder:
	if isinstance(raw, dict):
		model, backend, dim, pretrained = raw.get("model_name"), raw.get("backend"), raw.get("dim"), raw.get("pretrained")
		if (isinstance(model, str) and model and isinstance(backend, str) and backend and _is_int(dim) and dim > 0
				and (pretrained is None or isinstance(pretrained, str))):
			return Encoder(model, backend, dim, pretrained)
	raise QueryConfigurationError("the %s encoder in the scene graph's embedding provenance is malformed" % label)


def check_provenance(raw: Any) -> QueryProvenance:
	"""The query provenance stored in a graph's `embedding_provenance` metadata; QueryConfigurationError when it cannot be used."""
	if raw is None:
		raise QueryConfigurationError("the scene graph carries no embedding provenance: run Postprocess first")
	if not isinstance(raw, dict):
		raise QueryConfigurationError("the scene graph's embedding provenance is malformed")
	if not _is_int(raw.get("schema_version")) or raw["schema_version"] != PROVENANCE_SCHEMA_VERSION:
		raise QueryConfigurationError("the scene graph's embedding provenance has an unsupported version")
	if raw.get("sentence") is None:
		raise QueryConfigurationError("the scene graph has no sentence embeddings: run Postprocess first")
	clip = raw.get("clip")
	return QueryProvenance(_encoder(raw["sentence"], "sentence"), _encoder(clip, "CLIP") if clip is not None else None)


# ---------------------------------------------------------------------------------------------- readiness
@dataclass(frozen=True)
class QueryCapability:
	"""Whether a graph file can be asked questions, and why not; `provenance` is set whenever the graph states it validly."""
	ready: bool
	reason: Optional[str] = None
	provenance: Optional[QueryProvenance] = None

	def to_json(self) -> Dict[str, Any]:
		return {"ready": self.ready, "reason": self.reason, "provenance": self.provenance.to_json() if self.provenance is not None else None}


def _unreadable(error: BaseException) -> str:
	if isinstance(error, ArtifactError):
		return str(error)   # names the file, not its directory
	if isinstance(error, OSError):
		return error.strerror or type(error).__name__
	return type(error).__name__


def _embedded_objects(document: Dict[str, Any]) -> int:
	count = 0
	for node in document["nodes"]:
		if node["layer"] != 2 or node.get("partition", 0) not in (0, 2):   # objects and background objects
			continue
		metadata = (node.get("attributes") or {}).get("metadata")
		if isinstance(metadata, dict) and metadata.get("sentence_embedding_feature"):
			count += 1
	return count


def query_capability(graph_path: Path) -> QueryCapability:
	"""Read one graph file and say whether it can answer questions: sentence-embedding provenance and embedded objects.

	A raw mapping graph has neither (Postprocess adds them). Never raises; the reason names no host path.
	"""
	try:
		document, _ = load_valid_graph(Path(graph_path))
	except (ArtifactError, OSError, RecursionError, MemoryError) as error:
		return QueryCapability(False, "the scene graph cannot be read: %s" % _unreadable(error))
	metadata = document.get("metadata")
	try:
		provenance = check_provenance(metadata.get(PROVENANCE_KEY) if isinstance(metadata, dict) else None)
	except QueryConfigurationError as error:
		return QueryCapability(False, str(error))
	if not _embedded_objects(document):
		return QueryCapability(False, "no object of the scene graph has a sentence embedding: run Postprocess first", provenance)
	return QueryCapability(True, None, provenance)


# ---------------------------------------------------------------------------------------------- loading
def load_query_graph(graph_path: Path, dataset: DatasetContext, background_objects: Optional[Path] = None) -> Any:
	"""Load a scene graph the way the tools expect it: seconds since the dataset's time origin, only described objects.

	TUM graphs already hold seconds on the run's own clock and keep their timestamps. CODa graphs hold Unix times, which are
	shifted by the manifest's time origin, and their background objects are enriched from `background_objects` (default: the
	`background_objects.yaml` next to the graph, when there is one).
	"""
	import spark_dsg as sdsg
	from daaam.utils.evaluation import preprocess_scene_graph

	if not isinstance(dataset, DatasetContext):
		raise QueryConfigurationError("a dataset context is required to load a scene graph")
	graph_path = Path(graph_path)
	try:
		graph = sdsg.DynamicSceneGraph.load(str(graph_path))
	except Exception as error:   # spark_dsg reports every failure as a RuntimeError that names the file's directory
		raise QueryConfigurationError("the scene graph cannot be loaded (%s)" % type(error).__name__) from error
	if dataset.kind == "tum":
		return preprocess_scene_graph(graph, 0.0, None)
	background = Path(background_objects) if background_objects is not None else graph_path.parent / "background_objects.yaml"
	return preprocess_scene_graph(graph, dataset.time_origin, background if background.exists() else None)


# ---------------------------------------------------------------------------------------------- Agents
def agent_config(model_name: str, provenance: QueryProvenance) -> Any:
	"""The Agent configuration for a model: the four read-only tools and the encoders the graph was embedded with."""
	from daaam.scene_understanding.config import SceneUnderstandingConfig

	config = SceneUnderstandingConfig(model_name=model_name, available_tools=list(QUERY_TOOLS))
	config.tool_config.sentence_embedding_model_name = provenance.sentence.model_name
	# The default CLIP backend is "openai", which the provenance check rejects against a graph built with "openclip" or "pe".
	config.tool_config.clip_model_name = provenance.clip.model_name if provenance.clip is not None else None
	config.tool_config.clip_backend = provenance.clip.backend if provenance.clip is not None else None
	return config


def _make_sentence_handler(encoder: Encoder) -> Any:
	import torch
	from daaam.utils.embedding import SentenceEmbeddingHandler

	return SentenceEmbeddingHandler(model_name=encoder.model_name, device="cuda" if torch.cuda.is_available() else "cpu")


def _make_clip_handler(encoder: Encoder) -> Any:
	from daaam.utils.embedding import CLIPHandler

	return CLIPHandler(model_name=encoder.model_name, pretrained=encoder.pretrained, backend=encoder.backend)


class HandlerPool:
	"""The encoders of the Agents: built on first use and shared by every Agent, graph version and model that needs them.

	Each kind keeps the `limit` most recently used encoders; the heavy models are never built once per question or tab.
	"""

	def __init__(self, make_sentence: Optional[Callable[[Encoder], Any]] = None, make_clip: Optional[Callable[[Encoder], Any]] = None, limit: int = 2):
		self._make_sentence = make_sentence or _make_sentence_handler
		self._make_clip = make_clip or _make_clip_handler
		self._limit = max(1, limit)
		self._sentence: "OrderedDict[Encoder, Any]" = OrderedDict()
		self._clip: "OrderedDict[Encoder, Any]" = OrderedDict()
		self._lock = threading.RLock()

	def sentence(self, encoder: Encoder) -> Any:
		return self._get(self._sentence, encoder, self._make_sentence)

	def clip(self, encoder: Optional[Encoder]) -> Any:
		return None if encoder is None else self._get(self._clip, encoder, self._make_clip)

	def _get(self, store: "OrderedDict[Encoder, Any]", encoder: Encoder, make: Callable[[Encoder], Any]) -> Any:
		with self._lock:
			if encoder in store:
				store.move_to_end(encoder)
				return store[encoder]
			handler = make(encoder)
			store[encoder] = handler
			while len(store) > self._limit:
				store.popitem(last=False)
			return handler


def build_agent(model_name: str, provenance: QueryProvenance, graph: Any, handlers: HandlerPool, logger: Any = None) -> Any:
	"""A SceneUnderstandingAgent for one model over a loaded graph, using the pooled encoders."""
	from daaam.scene_understanding.services import SceneUnderstandingAgent

	sentence_handler = handlers.sentence(provenance.sentence)
	clip_handler = handlers.clip(provenance.clip)
	agent = SceneUnderstandingAgent(agent_config(model_name, provenance), logger, clip_handler=clip_handler, sentence_handler=sentence_handler)
	agent.update_scene_graph(graph)
	return agent


@dataclass
class _Entry:
	version: str
	dataset: DatasetContext
	graph: Any = field(repr=False)
	provenance: QueryProvenance
	agents: Dict[str, Any] = field(default_factory=dict, repr=False)


class AgentCache:
	"""The loaded graphs of the most recently asked results and one Agent per model over each.

	A result holds one graph version at a time: a new version replaces the old one together with its Agents, so a hot reload
	affects only the questions asked after it. A running question keeps the objects it was given even if they are evicted.
	"""

	def __init__(self, max_graphs: int = 2, loader: Optional[Callable[..., Any]] = None,
			factory: Optional[Callable[[str, QueryProvenance, Any, HandlerPool], Any]] = None,
			handlers: Optional[HandlerPool] = None, logger: Any = None):
		if not _is_int(max_graphs) or max_graphs < 1:
			raise ValueError("the cache needs room for at least one graph")
		self._max_graphs = max_graphs
		self._loader = loader or load_query_graph
		self._factory = factory or functools.partial(build_agent, logger=logger)
		self._handlers = handlers if handlers is not None else HandlerPool()
		self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
		self._lock = threading.RLock()   # building is slow and serialized: the worker answers one question at a time

	def __len__(self) -> int:
		with self._lock:
			return len(self._entries)

	def get(self, result_key: str, graph_path: Path, graph_version: str, dataset: DatasetContext, model_name: str,
			background_objects: Optional[Path] = None) -> Any:
		"""The Agent of `model_name` for this version of the result's graph, loading and building what is not cached yet."""
		with self._lock:
			entry = self._entries.get(result_key)
			if entry is not None and (entry.version, entry.dataset) != (graph_version, dataset):
				del self._entries[result_key]
				entry = None
			if entry is None:
				graph = self._loader(graph_path, dataset, background_objects=background_objects)
				metadata = dict(graph.metadata.get())
				entry = _Entry(graph_version, dataset, graph, check_provenance(metadata.get(PROVENANCE_KEY)))
				self._entries[result_key] = entry
			self._entries.move_to_end(result_key)
			agent = entry.agents.get(model_name)
			if agent is None:
				agent = self._factory(model_name, entry.provenance, entry.graph, self._handlers)
				entry.agents[model_name] = agent
			while len(self._entries) > self._max_graphs:
				self._entries.popitem(last=False)
			return agent

	def invalidate(self, result_key: str) -> None:
		with self._lock:
			self._entries.pop(result_key, None)
