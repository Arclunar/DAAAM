"""Finalize a mapping run into a versioned replay data bundle (RUN/replay/), the input of the replay workspace.

    recording (.replay-recording/snap_*.npz)  or  final graph (dsg.json, one snapshot)
        -> validate snapshots and times
        -> partition the snapshots into bounded, independently gzip-compressed JSON chunks (timeline/NNNNNN.json.gz)
        -> copy the camera images of every snapshot and the exact DAM description-source frames (images/...)
        -> optional static reference mesh (reference/reference.json.gz)
        -> write manifest.json last, validate the whole bundle, publish it with a directory rename

Everything is built in a temporary sibling `.replay.<pid>.tmp`; an existing bundle is moved aside and only deleted
after the new one is in place, and moved back when publishing fails, so a valid bundle is never lost. The bundle holds
data only: the application that renders it is the repository's single `replay.html` (`just replay-web`).

Dataset layouts (TUM, CODa) live in scripts/replay_datasets.py and are selected by the explicit dataset kind. Times in
the bundle are seconds since `dataset.time_origin` (TUM: the run's own clock, origin 0; CODa: the first stamp of
`timestamps/<seq>.txt`).
"""

import base64
import datetime
import gzip
import json
import os
import shutil
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from scripts import replay as core
from scripts.replay_datasets import dataset_assets
from scripts.replay_web.manifest import (
	GRAPH_CANDIDATES,
	MANIFEST_NAME,
	SCHEMA_VERSION,
	file_sha256,
	validate_manifest,
	write_json_atomic,
)

GENERATOR = {"name": "scripts/replay_bundle.py", "version": 1}
JPEG_QUALITY = 80
TIME_ORIGIN_TOLERANCE = 1e-3   # seconds: an explicit origin must agree with the dataset's own first stamp
_WRAPPER_BYTES = len(b'{"snapshots":[]}')


@dataclass(frozen=True)
class BundleOptions:
	recording: Optional[Path]   # directory written by `replay.py record`, or None for a final-graph bundle
	source_graph: Optional[Path]   # final dsg.json when no recording exists (CODa); needs frame_count
	run_dir: Path   # pipeline output directory (corrections.yaml: object descriptions and their source frames)
	output: Path   # the bundle directory to publish, normally RUN/replay
	dataset_kind: str   # "tum" or "coda", never inferred from a folder name
	sequence: str
	dataset: Optional[Path] = None   # dataset root for camera and description-source images
	fps: float = 15.0
	time_origin: Optional[float] = None   # None: the dataset's own origin (TUM 0, CODa first stamp)
	start_frame: int = 0
	frame_count: Optional[int] = None
	title: str = ""
	merge: float = 0.005
	thumb_width: int = 320
	max_depth: Optional[float] = None   # None: the dataset's default (TUM 4 m, CODa 30 m)
	reference_mesh: Optional[Path] = None
	max_chunk_snapshots: int = 24
	max_chunk_bytes: int = 8 * 1024 * 1024
	description_width: int = 960


def _chunk_body(records: Sequence[Dict[str, Any]]) -> bytes:
	return json.dumps({"snapshots": list(records)}, separators=(",", ":")).encode("utf-8")


def encode_chunk(records: Sequence[Dict[str, Any]]) -> bytes:
	"""One timeline chunk: gzip of {"snapshots": [...]}, deterministic (no time stamp in the gzip header)."""
	return gzip.compress(_chunk_body(records), compresslevel=6, mtime=0)


def _check(options: BundleOptions) -> None:
	if options.dataset_kind not in ("tum", "coda"):
		raise ValueError(f"unsupported dataset kind {options.dataset_kind!r}: expected tum or coda")
	if (options.recording is None) == (options.source_graph is None):
		raise ValueError("exactly one of recording or source_graph is required")
	if options.source_graph is not None and (options.frame_count is None or options.frame_count < 1):
		raise ValueError("a bundle from a final graph needs a positive frame_count (the run length places its single snapshot in time)")
	if not options.sequence:
		raise ValueError("the dataset sequence must be named")
	if options.fps <= 0 or options.start_frame < 0 or (options.frame_count is not None and options.frame_count < 1):
		raise ValueError("fps must be positive, start_frame >= 0 and frame_count positive")
	if options.max_chunk_snapshots < 1 or options.max_chunk_bytes < 1:
		raise ValueError("max_chunk_snapshots and max_chunk_bytes must be positive")


def _time_origin(options: BundleOptions, assets) -> float:
	own = assets.dataset_time_origin() if assets is not None else None
	if options.time_origin is None:
		if own is not None:
			return own
		if options.dataset_kind == "tum":
			return 0.0
		raise ValueError("a CODa bundle needs time_origin or a dataset with timestamps/<sequence>.txt")
	if own is not None and abs(options.time_origin - own) > TIME_ORIGIN_TOLERANCE:
		raise ValueError(f"time_origin {options.time_origin:.6f} disagrees with the dataset's first stamp {own:.6f}")
	return float(options.time_origin)


def _load_snapshots(options: BundleOptions, origin: float) -> Tuple[List[int], List[Dict[str, np.ndarray]], List[float]]:
	if options.recording is not None:
		numbers, snaps = core.load_snapshots(options.recording)
		if not snaps:
			raise ValueError(f"no snapshots in {options.recording}: the pipeline did not publish a scene graph (was it started with --zmq-url?)")
		times = [t - origin for t in core.estimate_times(numbers, snaps, options.fps)]
	else:
		import spark_dsg

		path = Path(options.source_graph)
		if not path.is_file():
			raise ValueError(f"no final scene graph at {path}")
		snaps = [core.extract_snapshot(spark_dsg.DynamicSceneGraph.load(str(path)))]
		last = options.start_frame + options.frame_count
		numbers = [last]
		times = [(last - 1) / options.fps]   # the last frame of the run
	times = [round(float(t), 3) for t in times]
	for i in range(1, len(times)):   # the estimate can wobble where graphs carry no stamp; playback needs order
		times[i] = max(times[i], times[i - 1])
	return numbers, snaps, times


def _jpeg(image: np.ndarray) -> bytes:
	import cv2

	ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
	if not ok:
		raise ValueError("JPEG encoding failed")
	return encoded.tobytes()


def _resized(image: np.ndarray, width: int, interpolation: int) -> np.ndarray:
	import cv2

	size = (width, max(1, round(image.shape[0] * width / image.shape[1])))
	return cv2.resize(image, size, interpolation=interpolation)


class _ImageWriter:
	"""Camera images of the snapshots and description-source frames, each dataset frame encoded once."""

	def __init__(self, bundle: Path, assets, options: BundleOptions, max_depth: float):
		self.bundle, self.assets, self.options, self.max_depth = bundle, assets, options, max_depth
		self.timeline: Dict[int, Tuple[Optional[str], Optional[str]]] = {}

	def _write(self, relative: str, data: bytes) -> str:
		path = self.bundle / relative
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(data)
		return relative

	def timeline_entry(self, time_since_origin: float) -> Dict[str, Any]:
		import cv2

		index = self.assets.timeline_index(time_since_origin)
		if index not in self.timeline:
			rgb_rel = depth_rel = None
			rgb_path = self.assets.rgb_path(index)
			image = cv2.imread(str(rgb_path)) if rgb_path is not None else None
			if image is not None:
				small = _resized(image, self.options.thumb_width, cv2.INTER_AREA)
				rgb_rel = self._write("images/timeline/rgb/%06d.jpg" % index, _jpeg(small))
				depth_path = self.assets.depth_path(index)
				raw = cv2.imread(str(depth_path), cv2.IMREAD_UNCHANGED) if depth_path is not None else None
				if raw is not None:
					raw = cv2.resize(raw, (small.shape[1], small.shape[0]), interpolation=cv2.INTER_NEAREST)
					scaled = np.clip(raw.astype(np.float32) / self.assets.depth_scale / self.max_depth * 255.0, 0, 255).astype(np.uint8)
					colour = cv2.applyColorMap(scaled, cv2.COLORMAP_JET)
					colour[raw == 0] = 0   # no measurement
					depth_rel = self._write("images/timeline/depth/%06d.jpg" % index, _jpeg(colour))
			self.timeline[index] = (rgb_rel, depth_rel)
		rgb_rel, depth_rel = self.timeline[index]
		return {"index": index, "rgb": rgb_rel, "depth": depth_rel}

	def description(self, source: Dict[str, Any]) -> Optional[Dict[str, Any]]:
		import cv2

		index = self.assets.description_index(source)
		path = self.assets.rgb_path(index) if index is not None else None
		image = cv2.imread(str(path)) if path is not None else None
		if image is None:
			return None
		if image.shape[1] > self.options.description_width:
			image = _resized(image, self.options.description_width, cv2.INTER_AREA)
		relative = self._write("images/descriptions/%06d.jpg" % index, _jpeg(image))
		return {"frame_id": int(source["frame_id"]), "timestamp": source.get("timestamp"), "camera": self.assets.camera, "index": index, "path": relative}


def _layers(snaps: Sequence[Dict[str, np.ndarray]]) -> List[str]:
	present = []
	for layer, key in (("mesh", "mesh_pos"), ("objects", "obj_cid"), ("places", "place_rad"), ("rooms", "room_pos"), ("buildings", "bld_pos"), ("agent", "agent_pos")):
		if any(len(snap.get(key, ())) for snap in snaps):
			present.append(layer)
	return present


def _write_chunks(bundle: Path, numbers: Sequence[int], snaps: Sequence[Dict[str, np.ndarray]], times: Sequence[float],
		options: BundleOptions) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
	meshes = [s["mesh_pos"] for s in snaps if len(s["mesh_pos"])]
	everything = np.concatenate(meshes) if meshes else np.zeros((1, 3), np.float32)
	qmin, qscale = core.quantization(everything)
	descriptors: List[Dict[str, Any]] = []
	pending: List[Dict[str, Any]] = []
	pending_bytes = 0
	first_snapshot = 0

	def flush() -> None:
		nonlocal pending, pending_bytes, first_snapshot
		body = _chunk_body(pending)
		payload = gzip.compress(body, compresslevel=6, mtime=0)
		relative = "timeline/%06d.json.gz" % len(descriptors)
		(bundle / "timeline").mkdir(parents=True, exist_ok=True)
		(bundle / relative).write_bytes(payload)
		descriptors.append({
			"index": len(descriptors),
			"path": relative,
			"first_snapshot": first_snapshot,
			"snapshot_count": len(pending),
			"t_first": pending[0]["t"],
			"t_last": pending[-1]["t"],
			"bytes": len(payload),
			"uncompressed_bytes": len(body),
			"sha256": file_sha256(bundle / relative),
		})
		first_snapshot += len(pending)
		pending, pending_bytes = [], 0

	for number, t, snap in zip(numbers, times, snaps):
		arrays = {k: v for k, v in snap.items() if k not in ("place_t", "agent_t")}
		arrays["mesh_pos"], _ = core.quantize_positions(snap["mesh_pos"], qmin, qscale)
		if len(snap["mesh_pos"]) < 65536:
			arrays["mesh_faces"] = snap["mesh_faces"].astype(np.uint16)
		record = {"n": int(number), "t": t, "p": base64.b64encode(zlib.compress(core.pack(arrays), 6)).decode()}
		size = len(json.dumps(record, separators=(",", ":")).encode("utf-8")) + (1 if pending else 0)
		if pending and (len(pending) >= options.max_chunk_snapshots or _WRAPPER_BYTES + pending_bytes + size > options.max_chunk_bytes):
			flush()
			size -= 1   # the first record of a chunk has no separating comma
		pending.append(record)
		pending_bytes += size
	flush()
	return descriptors, {"min": qmin.tolist(), "scale": qscale.tolist()}


def _build(bundle: Path, options: BundleOptions) -> Dict[str, Any]:
	reference_arrays = core.load_reference_mesh(options.reference_mesh) if options.reference_mesh else None
	assets = None
	if options.dataset is not None:
		assets = dataset_assets(options.dataset_kind, Path(options.dataset), sequence=options.sequence, fps=options.fps,
			start_frame=options.start_frame, frame_count=options.frame_count)
		first, end = assets.run_range()
		if end <= first:
			print(f"[replay] no {options.dataset_kind} images of this run in {options.dataset}: the bundle has no camera images", file=sys.stderr)
			assets = None
	origin = _time_origin(options, assets)
	numbers, snaps, times = _load_snapshots(options, origin)
	core.ensure_entity_ids(snaps)
	for snap in snaps:
		snap["mesh_pos"], snap["mesh_col"], snap["mesh_faces"] = core.merge_vertices(snap["mesh_pos"], snap["mesh_col"], snap["mesh_faces"], options.merge)

	bundle.mkdir(parents=True)
	chunks, quant = _write_chunks(bundle, numbers, snaps, times, options)
	run_dir = core.find_run_dir(Path(options.run_dir))
	sources = core.description_sources(run_dir)
	max_depth = options.max_depth if options.max_depth is not None else (assets.default_max_depth if assets is not None else 4.0)
	cameras: List[Dict[str, Any]] = []
	images: Dict[str, Any] = {"frame_total": 0, "timeline": [], "descriptions": {}}
	if assets is not None:
		writer = _ImageWriter(bundle, assets, options, max_depth)
		cameras = [
			{"key": "rgb", "label": "RGB", "sensor": assets.camera},
			{"key": "depth", "label": "深度", "sensor": assets.camera, "max_depth": max_depth, "depth_scale": assets.depth_scale},
		]
		images["frame_total"] = assets.frame_total
		images["timeline"] = [writer.timeline_entry(t) for t in times]
		for source in sources.values():
			key = str(source["frame_id"])
			if key not in images["descriptions"]:
				entry = writer.description(source)
				if entry is not None:
					images["descriptions"][key] = entry

	reference = None
	if reference_arrays is not None:
		payload = core.reference_payload(reference_arrays)
		data = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"), compresslevel=6, mtime=0)
		(bundle / "reference").mkdir()
		(bundle / "reference" / "reference.json.gz").write_bytes(data)
		reference = {
			"path": "reference/reference.json.gz",
			"bytes": len(data),
			"sha256": file_sha256(bundle / "reference" / "reference.json.gz"),
			"vertices": payload["vertices"],
			"triangles": payload["triangles"],
			"payload_bytes": payload["payload_bytes"],
			"label": payload["label"],
		}

	recording = str(options.recording) if options.recording is not None else None
	return {
		"schema_version": SCHEMA_VERSION,
		"generator": GENERATOR,
		"created_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
		"finalized": True,
		"title": options.title or (Path(options.run_dir).resolve().name),
		"dataset": {
			"kind": options.dataset_kind,
			"sequence": options.sequence,
			"time_origin": origin,
			"fps": float(options.fps),
			"start_frame": int(options.start_frame),
			"frame_count": options.frame_count,
			"source": str(options.dataset) if options.dataset is not None else None,
		},
		"timebase": {"unit": "s", "origin": "dataset.time_origin", "first": times[0], "last": times[-1]},
		"coordinates": {"frame": "world", "up": "z", "units": "m", "agent_quat": "world_R_body as [w, x, y, z]"},
		"quant": quant,
		"snapshots": {"count": len(times), "numbers": [int(n) for n in numbers], "times": list(times)},
		"chunks": chunks,
		"layers": _layers(snaps),
		"cameras": cameras,
		"images": images,
		"labels": core.label_names(run_dir),
		"description_sources": sources,
		"catalog": core.entity_catalog(snaps, times),
		"reference_mesh": reference,
		"graph_candidates": list(GRAPH_CANDIDATES),
		"build": {
			"recording": recording,
			"source_graph": str(options.source_graph) if options.source_graph is not None else None,
			"run_dir": str(options.run_dir),
			"merge": options.merge,
			"thumb_width": options.thumb_width,
			"description_width": options.description_width,
			"max_depth": max_depth,
			"max_chunk_snapshots": options.max_chunk_snapshots,
			"max_chunk_bytes": options.max_chunk_bytes,
			"reference_mesh": str(options.reference_mesh) if options.reference_mesh else None,
		},
	}


def _remove(path: Path) -> None:
	if path.is_symlink() or path.is_file():
		path.unlink()
	elif path.exists():
		shutil.rmtree(path)


def finalize_bundle(options: BundleOptions) -> Path:
	"""Build, validate and atomically publish the bundle; returns the path of its manifest.json."""
	_check(options)
	output = Path(options.output)
	output.parent.mkdir(parents=True, exist_ok=True)
	temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
	previous = output.with_name(f".{output.name}.{os.getpid()}.old")
	_remove(temporary)
	try:
		manifest = _build(temporary, options)
		write_json_atomic(temporary / MANIFEST_NAME, manifest)   # the commit point, written last
		validate_manifest(temporary, verify_digests=True)
		_remove(previous)
		had_previous = output.exists()
		if had_previous:
			os.replace(str(output), str(previous))
		try:
			os.replace(str(temporary), str(output))
		except OSError:
			if had_previous:
				os.replace(str(previous), str(output))
			raise
		_remove(previous)
	finally:
		_remove(temporary)
	return output / MANIFEST_NAME


def describe(manifest: Dict[str, Any], bundle: Path) -> str:
	"""One summary line for the command line."""
	chunk_bytes = sum(chunk["bytes"] for chunk in manifest["chunks"])
	images = sum(1 for entry in manifest["images"]["timeline"] if entry.get("rgb"))
	return (f"[replay] {manifest['dataset']['kind']} {manifest['dataset']['sequence']}: {manifest['snapshots']['count']} snapshots in "
		f"{len(manifest['chunks'])} chunks ({chunk_bytes / 1e6:.1f} MB), data time {manifest['timebase']['first']:.1f}-"
		f"{manifest['timebase']['last']:.1f} s, {len(manifest['labels'])} object descriptions, {images} camera images, "
		f"{len(manifest['images']['descriptions'])} description frames -> {bundle / MANIFEST_NAME}")
