"""Interactive Scene Graph Q&A demo.

Loads a DSG, sets up the SceneUnderstandingAgent, and runs a REPL
for free-form questions against the scene graph.
"""
import sys
from typing import Optional

import click
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # `scripts.replay_web.query`: the helper the replay chat uses

from daaam.utils.evaluation import START_TIMES

from daaam.scene_understanding.services import SceneUnderstandingAgent
from daaam.scene_understanding.config import SceneUnderstandingConfig
from daaam.scene_understanding.models import TextResponse
from daaam.utils.embedding import CLIPHandler, EmbeddingProvenance, PROVENANCE_KEY
from daaam.utils.logging import ConsoleLogger
from scripts.replay_web.query import PLACES_LAYERS, QUERY_TOOLS, DatasetContext, QueryConfigurationError, load_query_graph


def dataset_context(dataset_kind: Optional[str], seq_id: Optional[int], time_origin: Optional[float], sequence: Optional[str]) -> DatasetContext:
	"""The dataset a DSG belongs to. `--seq-id` alone keeps meaning a CODa sequence."""
	kind = dataset_kind or ("coda" if seq_id is not None else None)
	if kind is None:
		raise click.UsageError("name the dataset: --dataset-kind tum|coda (CODa also takes --seq-id)")
	if kind == "coda" and time_origin is None:
		if seq_id is None:
			raise click.UsageError("a CODa scene graph needs --seq-id (for its start time) or --time-origin")
		if seq_id not in START_TIMES:
			raise click.UsageError(f"Unknown seq_id {seq_id}. Valid: {sorted(START_TIMES.keys())}")
		time_origin = START_TIMES[seq_id]
	if kind == "tum":
		time_origin = 0.0 if time_origin is None else time_origin
	label = sequence or (str(seq_id) if seq_id is not None else kind)
	try:
		return DatasetContext(kind, label, time_origin, PLACES_LAYERS[kind])
	except QueryConfigurationError as error:
		raise click.UsageError(str(error))


@click.command(epilog="""\b
Examples:
  TUM  python scripts/demo_query.py --dsg-path output/tum_replay/out_.../clustered_dsg.json --dataset-kind tum
  CODa python scripts/demo_query.py --dsg-path output/coda/out_.../clustered_dsg_with_summaries.json --seq-id 0
""")
@click.option("--dsg-path", type=click.Path(exists=True), required=True)
@click.option("--dataset-kind", type=click.Choice(["tum", "coda"]), default=None,
	help="Dataset of the scene graph: tum keeps its run-relative timestamps, coda shifts its Unix times by the time origin. "
		"Defaults to coda when --seq-id is given.")
@click.option("--seq-id", type=int, default=None, help="CODA sequence ID (for START_TIMES lookup)")
@click.option("--time-origin", type=float, default=None,
	help="Dataset time of t = 0 in seconds. TUM: 0. CODa: the sequence's first stamp (default: START_TIMES[--seq-id]).")
@click.option("--sequence", type=str, default=None, help="Sequence label (informational), e.g. fr1_desk")
@click.option("--model-name", type=str, default="gpt-5-mini")
def main(dsg_path: str, dataset_kind: Optional[str], seq_id: Optional[int], time_origin: Optional[float], sequence: Optional[str], model_name: str):
	dataset = dataset_context(dataset_kind, seq_id, time_origin, sequence)
	try:
		sg = load_query_graph(Path(dsg_path), dataset)
	except QueryConfigurationError as error:
		raise click.ClickException(str(error))

	config = SceneUnderstandingConfig(
		model_name=model_name,
		available_tools=list(QUERY_TOOLS),
	)
	# Query with the CLIP encoder the scene graph was embedded with (as scripts/eval_navqa.py does): the tool config
	# names the backend "openai" by default, which the embedding provenance check rejects against "openclip" or "pe".
	clip_handler = None
	raw_provenance = dict(sg.metadata.get()).get(PROVENANCE_KEY)
	if raw_provenance is not None:
		clip = EmbeddingProvenance.from_metadata(raw_provenance).clip
		if clip is not None:
			config.tool_config.clip_model_name = clip.model_name
			config.tool_config.clip_backend = clip.backend
			clip_handler = CLIPHandler(model_name=clip.model_name, pretrained=clip.pretrained, backend=clip.backend)
	agent = SceneUnderstandingAgent(config, ConsoleLogger(), clip_handler=clip_handler)
	agent.update_scene_graph(sg)

	n_nodes = sum(1 for _ in sg.nodes)
	print(f"Scene graph loaded ({n_nodes} nodes, {dataset.kind}). Model: {model_name}")
	print("Type your question (empty line or Ctrl+C to quit).\n")

	while True:
		try:
			question = input("> ").strip()
		except (EOFError, KeyboardInterrupt):
			print()
			break
		if not question:
			break

		answer, _, _ = agent.answer_query(TextResponse, question)
		print(f"\nAnswer:    {answer.answer}")
		print(f"Reasoning: {answer.reasoning}\n")


if __name__ == "__main__":
	main()
