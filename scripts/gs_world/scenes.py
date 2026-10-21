"""Several trained gs_world models in one viewer: the catalog of models on disk, a layout that puts them side by side, and the
combined set of Gaussians that the viewer renders.

Models are found at <root>/<dataset>/<time>/model.pt, where scripts.gs_world.train writes them (/data/gs_world/models). The ids that
go to the browser are those two folder names, '<dataset>/<time>', never paths.

Every model lives in the world frame of its dataset (z up). The first model stays exactly where it is, so a single model is shown in
the frame of its dataset (the frame the renders and DAAAM use). Further models are moved horizontally by a rigid shift (rotating
Gaussians would also have to rotate their spherical harmonics) so that the scenes stand in a row along x with a gap between them;
each keeps its own floor height. The recorded camera poses move with their scene.
"""

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .data import Camera

MAX_SCENES = 6
GAP = 3.0  # metres between the footprints of neighbouring scenes
FOOTPRINT_MARGIN = 2.5  # metres around the camera path that the room itself is assumed to take


class SceneError(RuntimeError):
	"""A model that cannot be loaded (the message goes to the page)."""


@dataclass(frozen=True)
class SceneEntry:
	"""A model on disk, as far as its folder says (nothing is loaded)."""

	id: str
	path: Path
	dataset: str
	time: str
	gaussians: Optional[int]
	steps: Optional[int]
	psnr: Optional[float]  # held-out frames, rendered with the average appearance
	cc_psnr: Optional[float]  # the same after fitting the best colour transform
	ssim: Optional[float]
	size_mb: float
	mtime: float

	def to_json(self) -> Dict[str, Any]:
		return {"id": self.id, "dataset": self.dataset, "time": self.time, "gaussians": self.gaussians, "steps": self.steps,
			"psnr": self.psnr, "cc_psnr": self.cc_psnr, "ssim": self.ssim, "size_mb": self.size_mb, "mtime": self.mtime}


def _number(value: Any) -> Optional[float]:
	return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _read_json(path: Path) -> Dict[str, Any]:
	try:
		value = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return {}
	return value if isinstance(value, dict) else {}


def entry_for_folder(folder: Path) -> SceneEntry:
	"""The catalog entry of a model folder (with model.pt); its id is '<parent folder>/<folder>'. SceneError without model.pt."""
	folder = Path(folder)
	try:
		status = (folder / "model.pt").stat()
	except OSError as exc:
		raise SceneError(f"{folder} has no model.pt") from exc
	metrics = _read_json(folder / "metrics.json")
	mean = metrics.get("mean") if isinstance(metrics.get("mean"), dict) else {}
	gaussians, steps = _number(metrics.get("gaussians")), _number(metrics.get("steps"))
	return SceneEntry(
		id=f"{folder.parent.name}/{folder.name}", path=folder, dataset=folder.parent.name, time=folder.name,
		gaussians=int(gaussians) if gaussians is not None else None, steps=int(steps) if steps is not None else None,
		psnr=_number(mean.get("psnr")), cc_psnr=_number(mean.get("cc_psnr")), ssim=_number(mean.get("ssim")),
		size_mb=round(status.st_size / 1e6, 1), mtime=status.st_mtime)


def scan_models(root: Path) -> List[SceneEntry]:
	"""The models under root (<dataset>/<time>/model.pt), newest first."""
	entries = []
	for checkpoint in Path(root).glob("*/*/model.pt"):
		try:
			entries.append(entry_for_folder(checkpoint.parent))
		except SceneError:
			continue
	entries.sort(key=lambda entry: (-entry.mtime, entry.id))
	return entries


def pick_models(entries: Sequence[SceneEntry], ids: Sequence[str]) -> List[SceneEntry]:
	"""The entries with the given ids, in that order: KeyError for an unknown id, ValueError for none, a repeat or too many."""
	if not ids:
		raise ValueError("choose at least one scene")
	if len(ids) > MAX_SCENES:
		raise ValueError(f"at most {MAX_SCENES} scenes can be shown together")
	if len(set(ids)) != len(ids):
		raise ValueError("a scene can be chosen only once")
	by_id = {entry.id: entry for entry in entries}
	missing = [i for i in ids if i not in by_id]
	if missing:
		raise KeyError(f"no such scene: {missing[0]}")
	return [by_id[i] for i in ids]


def display_names(entries: Sequence[SceneEntry]) -> List[str]:
	"""Short names for a selection: the dataset, plus the hour and minute of the training when two scenes share a dataset."""
	counts: Dict[str, int] = {}
	for entry in entries:
		counts[entry.dataset] = counts.get(entry.dataset, 0) + 1
	return [entry.dataset if counts[entry.dataset] == 1 else f"{entry.dataset} {entry.time[9:13] or entry.time}" for entry in entries]


def footprint(poses: np.ndarray) -> Tuple[np.ndarray, float]:
	"""(centre [x, y], radius) of the floor area a scene takes: the camera path with a margin for the room around it."""
	centres = np.asarray(poses, dtype=np.float64)[:, :3, 3][:, :2]
	middle = np.median(centres, axis=0)
	radius = float(np.percentile(np.linalg.norm(centres - middle, axis=1), 95))
	return middle, max(radius, 1.0) + FOOTPRINT_MARGIN


def layout_offsets(poses_list: Sequence[np.ndarray], gap: float = GAP) -> List[np.ndarray]:
	"""Rigid shifts [dx, dy, 0] that stand the scenes in a row along +x, in the order given.

	The first scene is not moved; the others are moved so that their footprint centres lie on the line y = the first centre's y,
	and neighbouring footprints are `gap` metres apart.
	"""
	offsets: List[np.ndarray] = []
	row_y, previous_x, previous_radius = 0.0, 0.0, 0.0
	for i, poses in enumerate(poses_list):
		centre, radius = footprint(poses)
		if i == 0:
			row_y, x = float(centre[1]), float(centre[0])
			offsets.append(np.zeros(3))
		else:
			x = previous_x + previous_radius + radius + gap
			offsets.append(np.array([x - centre[0], row_y - centre[1], 0.0]))
		previous_x, previous_radius = x, radius
	return offsets


@dataclass
class Combined:
	"""The scenes in one world: Gaussians for the renderer, recorded poses in the shifted frame, and what is where."""

	params: Dict[str, Any]
	poses: np.ndarray
	scenes: List[Dict[str, Any]]  # per scene: id, name, start/count (cameras), held_out (local ids), offset, gaussians
	camera: Camera
	sh_degree: int

	@property
	def gaussians(self) -> int:
		return int(len(self.params["means"]))


def load_models(entries: Sequence[SceneEntry]) -> List[Dict[str, Any]]:
	"""Read the checkpoints onto the CPU: [{entry, params, meta}]; SceneError when one cannot be read."""
	from .model import load_checkpoint

	models = []
	for entry in entries:
		try:
			params, meta = load_checkpoint(entry.path / "model.pt", device="cpu")
		except Exception as exc:
			raise SceneError(f"cannot load {entry.id}: {type(exc).__name__}: {exc}") from exc
		models.append({"entry": entry, "params": params, "meta": meta})
	return models


def combine(models: Sequence[Dict[str, Any]], gap: float = GAP) -> Combined:
	"""One world from loaded models: Gaussians concatenated, the later scenes shifted, SH truncated to the lowest degree."""
	import torch

	from .model import PARAM_KEYS, sh_degree_of

	if not models:
		raise ValueError("no model to combine")
	poses_list = [np.asarray(model["meta"]["poses"], dtype=np.float64) for model in models]
	offsets = layout_offsets(poses_list, gap)
	degree = min(sh_degree_of(model["params"]) for model in models)
	rest = (degree + 1) ** 2 - 1
	names = display_names([model["entry"] for model in models])
	pieces: Dict[str, list] = {key: [] for key in PARAM_KEYS}
	poses, scenes, start = [], [], 0
	for model, shift, camera_poses, name in zip(models, offsets, poses_list, names):
		params = model["params"]
		for key in PARAM_KEYS:
			tensor = params[key]
			if key == "means":
				tensor = tensor + torch.as_tensor(shift, dtype=tensor.dtype, device=tensor.device)
			elif key == "shN":
				tensor = tensor[:, :rest]
			pieces[key].append(tensor)
		moved = camera_poses.copy()
		moved[:, :3, 3] += shift
		poses.append(moved)
		scenes.append({"id": model["entry"].id, "name": name, "start": start, "count": len(moved),
			"held_out": [int(i) for i in model["meta"].get("test_ids") or [] if 0 <= int(i) < len(moved)],
			"offset": [float(v) for v in shift], "gaussians": int(len(params["means"]))})
		start += len(moved)
	return Combined({key: torch.cat(parts, dim=0) for key, parts in pieces.items()}, np.concatenate(poses), scenes,
		Camera.from_info(models[0]["meta"]["camera"]), degree)


def title_of(names: Sequence[str]) -> str:
	return " + ".join(names) if len(names) <= 3 else " + ".join(names[:2]) + f" + {len(names) - 2} more"
