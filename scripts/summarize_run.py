#!/usr/bin/env python3
"""Print a short summary of one DAAAM pipeline output directory.

Usage:
	python scripts/summarize_run.py output/tum_fr1_desk/out_20261014_113949 [--max-objects 15]

The directory is the one printed as "Results saved to:" by scripts/run_pipeline.py (the inner out_*/ folder with
dsg.json, corrections.yaml and background_objects.yaml is found automatically). The summary covers the run
statistics, the number of nodes per scene graph layer, the semantic corrections (label -> DAM description) and
the object nodes of the scene graph together with their descriptions.
"""

import argparse
import json
import sys
from pathlib import Path

import yaml


def find_inner_dir(run_dir: Path) -> Path:
	"""The folder holding dsg.json / corrections.yaml (run_dir itself or its out_* child)."""
	if (run_dir / "dsg.json").exists():
		return run_dir
	for child in sorted(run_dir.glob("out_*")):
		if (child / "dsg.json").exists():
			return child
	raise SystemExit(f"no dsg.json found in {run_dir} or its out_* sub-directories")


def shorten(text: str, width: int) -> str:
	text = " ".join(str(text).split())
	return text if len(text) <= width else text[: width - 1] + "…"


def grounding_problems(log_dir: Path, limit: int = 5) -> list:
	"""Distinct ERROR messages of the grounding worker logs, e.g. DAM failing to load (the pipeline then runs on without it)."""
	messages = []
	for log_file in sorted(log_dir.glob("GroundingWorker-*.log")):
		for line in log_file.read_text(errors="replace").splitlines():
			if "| ERROR" in line:
				message = line.split("|", 3)[-1].strip()
				if message not in messages:
					messages.append(message)
	return messages[:limit]


def main() -> None:
	parser = argparse.ArgumentParser(description="Summarize a DAAAM output directory")
	parser.add_argument("run_dir", type=Path)
	parser.add_argument("--max-objects", type=int, default=15, help="How many object nodes to list")
	parser.add_argument("--width", type=int, default=110, help="Width of the description column")
	args = parser.parse_args()

	run_dir = args.run_dir.resolve()
	inner = find_inner_dir(run_dir)
	top = inner.parent if inner != run_dir and (inner.parent / "final_stats.json").exists() else run_dir

	print(f"run directory : {top}")
	stats_file = top / "final_stats.json"
	if stats_file.exists():
		stats = json.loads(stats_file.read_text())
		print(f"frames        : {stats.get('frames_processed')}/{stats.get('total_frames')}")
		for key, label in [("cv_avg_time", "CV time / frame (s)"), ("hydra_avg_time", "Hydra time / frame (s)")]:
			if key in stats:
				print(f"{label:22s}: {stats[key]:.3f}")

	import spark_dsg as dsg

	graph = dsg.DynamicSceneGraph.load(str(inner / "dsg.json"))
	print(f"\nscene graph   : {graph.num_nodes()} nodes, {graph.num_edges()} edges")
	for layer_id, name in [(dsg.DsgLayers.OBJECTS, "objects"), (dsg.DsgLayers.PLACES, "places"),
	                       (dsg.DsgLayers.ROOMS, "rooms"), (dsg.DsgLayers.BUILDINGS, "buildings")]:
		print(f"  {name:10s}: {graph.get_layer(layer_id).num_nodes()}")

	corrections = yaml.safe_load((inner / "corrections.yaml").read_text())
	descriptions = {entry["label"]: entry.get("name") for entry in corrections.get("label_names", [])
	                if entry.get("name") not in (None, "unknown")}
	print(f"\ncorrections   : {len(corrections.get('label_names', []))} labels, {len(descriptions)} with a DAM description")
	problems = grounding_problems(top / "logs")
	if not descriptions or problems:
		print("WARNING       : " + ("no object got a DAM description" if not descriptions else "the grounding worker logged errors"))
		for message in problems:
			print(f"                {shorten(message, 160)}")

	background_file = inner / "background_objects.yaml"
	if background_file.exists():
		background = yaml.safe_load(background_file.read_text())
		print("background    : " + ", ".join(f"{k}={background[k]}" for k in
		                                    ("total_background_objects", "total_filtered", "total_in_hydra") if k in background))

	objects = list(graph.get_layer(dsg.DsgLayers.OBJECTS).nodes)
	print(f"\nobject nodes (first {min(args.max_objects, len(objects))} of {len(objects)}):")
	for node in objects[: args.max_objects]:
		attrs = node.attributes
		x, y, z = (round(float(v), 2) for v in attrs.position)
		description = descriptions.get(attrs.semantic_label, "<no description>")
		print(f"  {str(node.id):7s} label={attrs.semantic_label:<5d} pos=({x:6.2f},{y:6.2f},{z:5.2f})  {shorten(description, args.width)}")


if __name__ == "__main__":
	sys.exit(main())
