"""Compare the object layers of DAAAM results that were mapped in the same world frame.

	docker compose run --rm -T daaam-ws python -m scripts.gs_world.compare_maps REFERENCE OTHER [OTHER ...]
	(just gs-compare REFERENCE OTHER ...)

A result is an output/<name> folder (its newest out_* run is used) or an out_* folder. The first one is the reference.
For every other result the report gives the node counts, the objects matched one-to-one with a reference object
within --radius metres (greedy by distance), the median distance of the matched pairs, and how much the DAM
descriptions of matched pairs agree (Jaccard index of their content words, next to the same index for random pairs as
the chance level), with examples. The results must share the world frame: true for runs on a recorded dataset and on
renders of a gs_world model of it (gs_world.render keeps the dataset's world frame, also for offset trajectories).
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

STOPWORDS = frozenset("""
a an the and or of with without in on at to from by for as is are be been being it its this that these those has have
having which who whose while into onto over under above below near between their there here very slightly possibly
appears appear seems seem visible featuring features feature some several small large one two three part parts top
side sides front back edge edges surface shape shaped design finish color colour overall slight mostly likely
""".split())


def content_words(text: str) -> set:
	return {w for w in re.findall(r"[a-z]+", (text or "").lower()) if w not in STOPWORDS and len(w) > 2}


def jaccard(a: str, b: str) -> float:
	wa, wb = content_words(a), content_words(b)
	union = wa | wb
	return len(wa & wb) / len(union) if union else 0.0


def match_objects(reference: np.ndarray, other: np.ndarray, radius: float) -> List[Tuple[int, int, float]]:
	"""One-to-one pairs (reference index, other index, distance) within radius, closest pairs first."""
	if len(reference) == 0 or len(other) == 0:
		return []
	distances = np.linalg.norm(reference[:, None, :] - other[None, :, :], axis=2)
	candidates = np.argwhere(distances <= radius)
	order = np.argsort(distances[candidates[:, 0], candidates[:, 1]], kind="stable")
	used_ref, used_other, pairs = set(), set(), []
	for i, j in candidates[order]:
		if i in used_ref or j in used_other:
			continue
		used_ref.add(int(i))
		used_other.add(int(j))
		pairs.append((int(i), int(j), float(distances[i, j])))
	return pairs


def run_folder(path: Path) -> Path:
	"""The folder with dsg.json: the path itself or its newest out_* child."""
	path = Path(path)
	if (path / "dsg.json").is_file():
		return path
	runs = sorted((p for p in path.glob("out_*") if (p / "dsg.json").is_file()), key=lambda p: p.name)
	if not runs:
		raise SystemExit(f"no dsg.json in {path} or its out_* folders")
	return runs[-1]


def load_result(path: Path) -> Dict:
	"""Object positions and DAM descriptions plus the layer sizes of one DAAAM result."""
	import spark_dsg as dsg
	import yaml

	folder = run_folder(path)
	graph = dsg.DynamicSceneGraph.load(str(folder / "dsg.json"))
	corrections = yaml.safe_load((folder / "corrections.yaml").read_text()) if (folder / "corrections.yaml").is_file() else {}
	names = {e["label"]: e.get("name") for e in (corrections or {}).get("label_names", [])
		if e.get("name") not in (None, "unknown")}
	nodes = list(graph.get_layer(dsg.DsgLayers.OBJECTS).nodes)
	positions = np.array([[float(v) for v in n.attributes.position] for n in nodes], dtype=np.float64).reshape(-1, 3)
	descriptions = [names.get(n.attributes.semantic_label, "") for n in nodes]
	counts = {name: graph.get_layer(layer).num_nodes() for name, layer in (("objects", dsg.DsgLayers.OBJECTS),
		("places", dsg.DsgLayers.PLACES), ("rooms", dsg.DsgLayers.ROOMS))}
	counts["described_labels"] = len(names)
	return {"run": str(folder), "positions": positions, "descriptions": descriptions, "counts": counts}


def compare(reference: Dict, other: Dict, radius: float, examples: int = 5, seed: int = 0) -> Dict:
	pairs = match_objects(reference["positions"], other["positions"], radius)
	n_ref, n_other = len(reference["positions"]), len(other["positions"])
	described = [(i, j, d) for i, j, d in pairs if reference["descriptions"][i] and other["descriptions"][j]]
	agreement = [jaccard(reference["descriptions"][i], other["descriptions"][j]) for i, j, _ in described]
	rng = np.random.default_rng(seed)
	ref_text = [t for t in reference["descriptions"] if t]
	other_text = [t for t in other["descriptions"] if t]
	chance = [jaccard(ref_text[rng.integers(len(ref_text))], other_text[rng.integers(len(other_text))])
		for _ in range(2000)] if ref_text and other_text else []
	shown = sorted(described, key=lambda p: p[2])[:examples]
	return {
		"run": other["run"],
		"counts": other["counts"],
		"matched": len(pairs),
		"matched_share_of_reference": len(pairs) / n_ref if n_ref else 0.0,
		"matched_share_of_run": len(pairs) / n_other if n_other else 0.0,
		"median_distance_m": float(np.median([d for _, _, d in pairs])) if pairs else None,
		"description_jaccard": float(np.mean(agreement)) if agreement else None,
		"random_pair_jaccard": float(np.mean(chance)) if chance else None,
		"examples": [{"distance_m": round(d, 3), "reference": reference["descriptions"][i], "run": other["descriptions"][j]}
			for i, j, d in shown],
	}


def short(text: str, width: int = 90) -> str:
	text = " ".join((text or "").split())
	return text if len(text) <= width else text[: width - 1] + "…"


def main(argv: Optional[Sequence[str]] = None) -> int:
	p = argparse.ArgumentParser(description="Compare the object layers of DAAAM results in one world frame.")
	p.add_argument("reference", type=Path)
	p.add_argument("others", type=Path, nargs="+")
	p.add_argument("--radius", type=float, default=0.3, help="max distance (m) of matched objects")
	p.add_argument("--examples", type=int, default=5, help="matched pairs to print per result")
	p.add_argument("--json", type=Path, default=None, help="also write the report as JSON")
	args = p.parse_args(argv)

	reference = load_result(args.reference)
	c = reference["counts"]
	print(f"reference {reference['run']}: {c['objects']} objects, {c['places']} places, {c['rooms']} rooms, "
		f"{c['described_labels']} described labels")
	report = {"reference": {"run": reference["run"], "counts": c}, "radius_m": args.radius, "results": []}
	for path in args.others:
		result = compare(reference, load_result(path), args.radius, args.examples)
		report["results"].append(result)
		rc = result["counts"]
		median = f"{result['median_distance_m']:.3f} m" if result["median_distance_m"] is not None else "-"
		agree = (f"{result['description_jaccard']:.3f} (random pairs {result['random_pair_jaccard']:.3f})"
			if result["description_jaccard"] is not None else "-")
		print(f"\n{result['run']}: {rc['objects']} objects, {rc['places']} places, {rc['rooms']} rooms, "
			f"{rc['described_labels']} described labels")
		print(f"  matched within {args.radius:g} m: {result['matched']} = {result['matched_share_of_reference']:.1%} of the "
			f"reference objects, {result['matched_share_of_run']:.1%} of its own; median distance {median}")
		print(f"  description agreement of matched pairs (Jaccard of content words): {agree}")
		for example in result["examples"]:
			print(f"    {example['distance_m']:.2f} m  ref: {short(example['reference'])}")
			print(f"            run: {short(example['run'])}")
	if args.json:
		args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
		print(f"\nreport: {args.json}")
	return 0


if __name__ == "__main__":
	sys.exit(main())
