#!/usr/bin/env python3
"""Adapt ordinary Hydra PLACES nodes for the traversability-only cluster tool."""

import argparse
import copy
import json
from pathlib import Path
from typing import Any, Dict, Tuple


COMMON_ATTRIBUTE_KEYS = (
	"bounding_box",
	"color",
	"distance",
	"is_active",
	"is_predicted",
	"label_weights",
	"last_update_time_ns",
	"metadata",
	"name",
	"position",
	"semantic_feature",
	"semantic_label",
)
CLUSTERED_ATTRIBUTE_KEYS = ("color", "distance", "is_predicted", "semantic_label")


def load_graph(path: Path) -> Dict[str, Any]:
	with path.open("r", encoding="utf-8") as stream:
		return json.load(stream)


def save_graph(graph: Dict[str, Any], path: Path) -> None:
	with path.open("w", encoding="utf-8") as stream:
		json.dump(graph, stream, separators=(",", ":"))


def layer_key(graph: Dict[str, Any], name: str) -> Tuple[int, int]:
	try:
		key = graph["layer_names"][name]
		return int(key["layer"]), int(key["partition"])
	except (KeyError, TypeError, ValueError) as error:
		raise ValueError(f"scene graph has no layer named {name}") from error


def is_layer_node(node: Dict[str, Any], key: Tuple[int, int]) -> bool:
	return (node.get("layer"), node.get("partition", 0)) == key


def as_traversability_attributes(source: Dict[str, Any]) -> Dict[str, Any]:
	attrs = {name: copy.deepcopy(source[name]) for name in COMMON_ATTRIBUTE_KEYS if name in source}
	observed_ns = int(source.get("last_update_time_ns", 0))
	attrs.update(
		{
			"type": "TraversabilityNodeAttributes",
			"first_observed_ns": observed_ns,
			"last_observed_ns": observed_ns,
			"min": [-1.0, -1.0],
			"max": [1.0, 1.0],
			"states_0": [1],
			"states_1": [1],
			"states_2": [1],
			"states_3": [1],
			"vmf_feature_sum": None,
			"vmf_observation_count": 0,
		}
	)
	return attrs


def prepare(src: Path, dst: Path, layer: str) -> None:
	graph = load_graph(src)
	key = layer_key(graph, layer)
	for node in graph.get("nodes", []):
		if is_layer_node(node, key):
			node["attributes"] = as_traversability_attributes(node.get("attributes", {}))
	save_graph(graph, dst)


def restore(src: Path, original: Path, dst: Path, layer: str) -> None:
	clustered = load_graph(src)
	before = load_graph(original)
	key = layer_key(before, layer)
	original_attrs = {
		node["id"]: node.get("attributes", {})
		for node in before.get("nodes", [])
		if is_layer_node(node, key)
	}
	for node in clustered.get("nodes", []):
		if not is_layer_node(node, key) or node.get("id") not in original_attrs:
			continue
		clustered_attrs = node.get("attributes", {})
		attrs = copy.deepcopy(original_attrs[node["id"]])
		for name in CLUSTERED_ATTRIBUTE_KEYS:
			if name in clustered_attrs:
				attrs[name] = copy.deepcopy(clustered_attrs[name])
		node["attributes"] = attrs
	save_graph(clustered, dst)


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description=__doc__)
	subparsers = parser.add_subparsers(dest="command", required=True)
	for command in ("prepare", "restore"):
		subparser = subparsers.add_parser(command)
		subparser.add_argument("--src", type=Path, required=True)
		subparser.add_argument("--dst", type=Path, required=True)
		subparser.add_argument("--layer", default="PLACES")
		if command == "restore":
			subparser.add_argument("--original", type=Path, required=True)
	return parser.parse_args()


def main() -> None:
	args = parse_args()
	if args.command == "prepare":
		prepare(args.src, args.dst, args.layer)
	else:
		restore(args.src, args.original, args.dst, args.layer)


if __name__ == "__main__":
	main()
