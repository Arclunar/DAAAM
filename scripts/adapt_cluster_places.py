#!/usr/bin/env python3
"""Adapt ordinary Hydra PLACES nodes for the traversability-only cluster tool."""

import argparse
import copy
import json
from collections import Counter, defaultdict
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
	# Ordinary PLACES do not accumulate the image-projected label_weights used
	# by CODa's traversability nodes. Recover labels from explicit object edges,
	# counting attached objects per label (not a guessed spatial association).
	features = graph.get("metadata", {}).get("features", {})
	object_key = layer_key(graph, "OBJECTS") if "OBJECTS" in graph.get("layer_names", {}) else (2, 0)
	nodes = {node["id"]: node for node in graph.get("nodes", [])}
	attached = defaultdict(Counter)
	for edge in graph.get("edges", []):
		a, b = nodes.get(edge["source"]), nodes.get(edge["target"])
		if a is None or b is None:
			continue
		if is_layer_node(b, key):
			a, b = b, a
		if not is_layer_node(a, key) or not is_layer_node(b, object_key):
			continue
		label = b.get("attributes", {}).get("semantic_label")
		feature = features.get(str(label), {})
		if feature.get("clip_feature") and feature.get("sentence_embedding_feature"):
			attached[a["id"]][label] += 1.0
	usable = 0
	for node in graph.get("nodes", []):
		if is_layer_node(node, key):
			attrs = as_traversability_attributes(node.get("attributes", {}))
			if not attrs.get("label_weights"):
				attrs["label_weights"] = sorted(attached[node["id"]].items())
			usable += bool(attrs["label_weights"])
			node["attributes"] = attrs
	if not usable:
		raise ValueError("No usable place labels: expected label_weights or attached objects with CLIP and sentence features")
	print(f"Prepared {layer}: {usable} places with semantic label weights")
	save_graph(graph, dst)


def restore(src: Path, original: Path, dst: Path, layer: str) -> None:
	clustered = load_graph(src)
	before = load_graph(original)
	key = layer_key(before, layer)
	room_key = layer_key(clustered, "ROOMS")
	if not any(is_layer_node(node, room_key) for node in clustered.get("nodes", [])):
		raise ValueError("Clustering produced no rooms; previous output is preserved. Check place labels and clustering thresholds")
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


def validate(src: Path, layer: str, require_rooms: bool = False) -> None:
	"""Reject empty native semantic inputs/results before a stage publishes them."""
	graph = load_graph(src)
	key = layer_key(graph, layer)
	features = graph.get("metadata", {}).get("features", {})
	usable = 0
	for node in graph.get("nodes", []):
		if not is_layer_node(node, key):
			continue
		weights = node.get("attributes", {}).get("label_weights", [])
		if isinstance(weights, dict):
			weights = weights.items()
		usable += any(weight > 0 and features.get(str(label), {}).get("clip_feature")
			and features.get(str(label), {}).get("sentence_embedding_feature") for label, weight in weights)
	if not usable:
		raise ValueError(f"No native semantic {layer} places with usable features. Check observed surfaces and completed DAM descriptions; old TUM runs need the handheld-camera profile")
	if require_rooms:
		room_key = layer_key(graph, "ROOMS")
		if not any(is_layer_node(node, room_key) for node in graph.get("nodes", [])):
			raise ValueError("Native place clustering produced no rooms; check semantic coverage and clustering thresholds")
	print(f"Validated {usable} native semantic {layer} places")


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
	validator = subparsers.add_parser("validate")
	validator.add_argument("--src", type=Path, required=True)
	validator.add_argument("--layer", default="TRAVERSABILITY")
	validator.add_argument("--require-rooms", action="store_true")
	return parser.parse_args()


def main() -> None:
	args = parse_args()
	if args.command == "prepare":
		prepare(args.src, args.dst, args.layer)
	elif args.command == "validate":
		validate(args.src, args.layer, args.require_rooms)
	else:
		restore(args.src, args.original, args.dst, args.layer)


if __name__ == "__main__":
	main()
