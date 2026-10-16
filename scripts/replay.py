#!/usr/bin/env python3
"""Review a mapping run without RViz: record the scene graph while Hydra builds it, then finalize the recording into the
run's replay data bundle, which the replay workspace (`just replay-web`, the repository's single replay.html) plays.

Hydra publishes the whole scene graph (mesh included) over ZMQ after every backend update, about once per frame; that
stream is what RViz shows. `record` subscribes to it and keeps every n-th graph as a compact snapshot. `finalize` turns
the snapshots into RUN/replay/ (scripts/replay_bundle.py: bounded gzip timeline chunks, camera and DAM source images, a
manifest written last); `rrd` writes the same timed snapshots to a native Rerun recording. Subscribing costs the pipeline
nothing measurable; RViz is what slows a live run down.

scripts/run_replay.sh does all of this around a TUM pipeline run. By hand:
	python scripts/replay.py record --out /tmp/rec [--zmq-url tcp://127.0.0.1:8001] [--every 5]   # stop with Ctrl-C
	python scripts/replay.py finalize /tmp/rec --run output/NAME/out_YYYYMMDD_HHMMSS --dataset-kind tum --sequence fr1_desk \
		--dataset datasets/tum_fr1_desk --fps 15 --out output/NAME/out_YYYYMMDD_HHMMSS/replay
	python scripts/replay.py rrd /tmp/rec --run output/NAME/out_YYYYMMDD_HHMMSS --out replay.rrd
A run without a recording (CODa) is finalized from its final graph: `finalize --graph RUN/dsg.json --dataset-kind coda
--sequence 0 --start-frame S --frame-count N ...` (scripts/run_coda_example.sh passes the bag's daaam_replay.json).
With --dataset the bundle also holds the RGB and depth image of the frame the pipeline had processed at each moment and the
exact frames DAM described objects from. With --reference-mesh CACHE.npz (written by scripts/build_reference_mesh.py, the
1 cm Open3D TSDF reference mesh of the whole dataset) the viewer can switch between that static mesh and Hydra's
time-varying one; it is stored once, as a member of its own. Open3D is not needed to use a cache.
"""

import argparse
import base64
import json
import signal
import struct
import sys
import zipfile
import zlib
from pathlib import Path

import numpy as np

DEFAULT_URL = "tcp://127.0.0.1:8001"


# ---------------------------------------------------------------- record

def extract_snapshot(graph) -> dict:
	"""Arrays describing one scene graph: what the web page draws."""
	snap = {
		"mesh_pos": np.zeros((0, 3), np.float32), "mesh_col": np.zeros((0, 3), np.uint8), "mesh_faces": np.zeros((0, 3), np.uint32),
		"obj_cid": np.zeros(0, np.int32), "obj_label": np.zeros(0, np.uint32), "obj_min": np.zeros((0, 3), np.float32), "obj_max": np.zeros((0, 3), np.float32),
		"place_cid": np.zeros(0, np.int32), "place_pos": np.zeros((0, 3), np.float32), "place_rad": np.zeros(0, np.float32), "place_edges": np.zeros((0, 2), np.int32),
		"room_pos": np.zeros((0, 3), np.float32), "room_place_edges": np.zeros((0, 2), np.int32),
		"bld_pos": np.zeros((0, 3), np.float32), "bld_room_edges": np.zeros((0, 2), np.int32),
		"obj_place_edges": np.zeros((0, 2), np.int32),
		"agent_pos": np.zeros((0, 3), np.float32), "agent_quat": np.zeros((0, 4), np.float32),
	}

	if graph.has_mesh() and graph.mesh.num_vertices() > 0:
		mesh = graph.mesh
		vertices = mesh.get_vertices()  # (6, N): xyz and rgb in [0, 1]
		faces = mesh.get_faces().T.astype(np.uint32)
		faces = faces[(faces < vertices.shape[1]).all(axis=1)]
		faces = faces[(faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])]
		snap["mesh_pos"] = np.ascontiguousarray(vertices[:3].T, dtype=np.float32)
		snap["mesh_col"] = np.clip(vertices[3:6].T * 255.0, 0, 255).astype(np.uint8)
		snap["mesh_faces"] = faces

	# node symbols: O objects, p places, R rooms, B buildings, a agent poses
	by_category = {}
	for node in graph.nodes:
		by_category.setdefault(node.id.category, []).append(node)
	index = {}  # node id value -> (category, position in that category's arrays)
	for category, nodes in by_category.items():
		for i, node in enumerate(nodes):
			index[node.id.value] = (category, i)

	objects = by_category.get("O", [])
	if objects:
		boxes = []
		for node in objects:
			attrs = node.attributes
			box = attrs.bounding_box
			if box.is_valid() and box.has_rotation():
				corners = np.asarray(box.corners(), np.float32)
				corners = corners.T if corners.shape[0] == 3 else corners
				boxes.append((corners.min(axis=0), corners.max(axis=0)))
			elif box.is_valid():
				boxes.append((np.asarray(box.min, np.float32), np.asarray(box.max, np.float32)))
			else:
				center = np.asarray(attrs.position, np.float32)
				boxes.append((center - 0.05, center + 0.05))
		snap["obj_cid"] = np.array([node.id.category_id for node in objects], np.int32)
		snap["obj_label"] = np.array([node.attributes.semantic_label for node in objects], np.uint32)
		snap["obj_min"] = np.stack([b[0] for b in boxes])
		snap["obj_max"] = np.stack([b[1] for b in boxes])

	places = by_category.get("p", [])
	if places:
		snap["place_cid"] = np.array([node.id.category_id for node in places], np.int32)
		snap["place_pos"] = np.stack([np.asarray(node.attributes.position, np.float32) for node in places])
		snap["place_rad"] = np.array([node.attributes.distance for node in places], np.float32)
		pairs = [(index[e.source][1], index[e.target][1]) for e in graph.get_layer("PLACES").edges
		         if index.get(e.source, ("", 0))[0] == "p" and index.get(e.target, ("", 0))[0] == "p"]
		snap["place_edges"] = np.array(pairs, np.int32).reshape(-1, 2)
	for key, category in (("room_pos", "R"), ("bld_pos", "B")):
		nodes = by_category.get(category, [])
		if nodes:
			snap[key] = np.stack([np.asarray(node.attributes.position, np.float32) for node in nodes])

	# edges between layers, as (parent index, child index) pairs
	wanted = {("R", "p"): "room_place_edges", ("B", "R"): "bld_room_edges", ("p", "O"): "obj_place_edges"}
	collected = {key: [] for key in wanted.values()}
	for edge in graph.interlayer_edges:
		source, target = index.get(edge.source), index.get(edge.target)
		if source is None or target is None:
			continue
		for a, b in ((source, target), (target, source)):
			if (a[0], b[0]) in wanted:
				collected[wanted[(a[0], b[0])]].append((a[1], b[1]))
	for key, pairs in collected.items():
		snap[key] = np.array(pairs, np.int32).reshape(-1, 2)

	agents = sorted(by_category.get("a", []), key=lambda node: node.attributes.timestamp)
	if agents:
		snap["agent_pos"] = np.stack([np.asarray(node.attributes.position, np.float32) for node in agents])
		snap["agent_quat"] = np.array([[q.w, q.x, q.y, q.z] for q in (node.attributes.world_R_body for node in agents)], np.float32)
		snap["agent_t"] = np.array([node.attributes.timestamp.total_seconds() for node in agents], np.float64)
	if places:
		snap["place_t"] = np.array([node.attributes.last_update_time_ns / 1e9 for node in places], np.float64)
	return snap


def record(args) -> int:
	import spark_dsg as dsg
	import zmq

	out = Path(args.out)
	out.mkdir(parents=True, exist_ok=True)
	sock = zmq.Context.instance().socket(zmq.SUB)
	sock.connect(args.zmq_url)
	sock.setsockopt_string(zmq.SUBSCRIBE, "")

	stop = []
	signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
	signal.signal(signal.SIGINT, lambda *_: stop.append(1))

	def save(buf, n):
		snap = extract_snapshot(dsg.DynamicSceneGraph.from_binary(buf))
		np.savez(out / f"snap_{n:06d}.npz", **snap)

	messages, saved, last, last_saved = 0, 0, None, 0
	print(f"[replay] recording {args.zmq_url} -> {out} (every {args.every}. graph)", flush=True)
	while not stop:
		if not sock.poll(200):
			continue
		last = sock.recv()
		messages += 1
		if messages == 1 or messages % args.every == 0:
			save(last, messages)
			saved, last_saved = saved + 1, messages
	while sock.poll(300):  # graphs still queued when the run ended
		last = sock.recv()
		messages += 1
	if last is not None and last_saved != messages:
		save(last, messages)  # the final state always ends the recording
		saved += 1
	(out / "meta.json").write_text(json.dumps({"zmq_url": args.zmq_url, "every": args.every, "messages": messages, "snapshots": saved}))
	print(f"[replay] {messages} graphs received, {saved} snapshots saved", flush=True)
	return 0


# ---------------------------------------------------------------- snapshot payloads (scripts/replay_bundle.py)

DTYPE_CODES = {"<f4": "f4", "<i4": "i4", "<u4": "u4", "<i2": "i2", "<u2": "u2", "|u1": "u1"}


def pack(arrays: dict) -> bytes:
	"""[uint32 header length][JSON header][arrays, each 8-byte aligned]: read by unpack() in scripts/replay_web/static/replay_data.js."""
	header, blobs, offset = [], [], 0
	for name, array in arrays.items():
		array = np.ascontiguousarray(array)
		pad = (-offset) % 8
		blobs.append(b"\0" * pad)
		offset += pad
		header.append({"n": name, "t": DTYPE_CODES[array.dtype.str], "s": list(array.shape), "o": offset})
		raw = array.tobytes()
		blobs.append(raw)
		offset += len(raw)
	head = json.dumps(header, separators=(",", ":")).encode()
	head += b" " * ((-(4 + len(head))) % 8)
	return struct.pack("<I", len(head)) + head + b"".join(blobs)


REFERENCE_ARRAYS = {"positions": ("float32", 3), "colors": ("uint8", 3), "normals": ("float32", 3), "triangles": ("uint32", 3)}
REFERENCE_LABEL = "1 cm 全序列参考网格"


def quantization(points):
	"""Origin and step (float64, 3 each) of a grid with 65536 steps per axis that spans the points."""
	origin = points.min(axis=0).astype(np.float64)
	scale = (points.max(axis=0) - origin) / 65535.0
	scale[scale == 0] = 1.0
	return origin, scale


def quantize_positions(points, origin=None, scale=None):
	"""Positions as int16 on a quantization grid (the given one, or one that spans the points) and that grid as {min, scale}:
	the page restores a position as min + (int16 + 32768) * scale."""
	if origin is None:
		origin, scale = quantization(points)
	quantized = np.clip(np.round((points - origin) / scale - 32768.0), -32768, 32767).astype(np.int16)
	return quantized, {"min": origin.tolist(), "scale": scale.tolist()}


def load_reference_mesh(path):
	"""The arrays of a reference-mesh cache (scripts/build_reference_mesh.py), checked; ValueError names the file when it is
	unreadable or is not a finite, non-empty indexed triangle mesh. Reading a cache needs no Open3D."""
	path = Path(path)
	try:
		with np.load(path, allow_pickle=False) as data:
			arrays = {name: data[name] for name in data.files}
	except (OSError, ValueError, zipfile.BadZipFile, EOFError) as error:
		raise ValueError(f"{path}: cannot read the reference mesh cache ({error})") from error
	try:
		for name, (dtype, width) in REFERENCE_ARRAYS.items():
			if name not in arrays:
				raise ValueError(f"missing array '{name}'")
			if arrays[name].ndim != 2 or arrays[name].shape[1] != width:
				raise ValueError(f"'{name}' must have shape (N, {width}), got {tuple(arrays[name].shape)}")
			if arrays[name].dtype != np.dtype(dtype):
				raise ValueError(f"'{name}' must be {dtype}, got {arrays[name].dtype}")
		count = len(arrays["positions"])
		if count == 0 or len(arrays["triangles"]) == 0:
			raise ValueError("the mesh is empty")
		if len(arrays["colors"]) != count or len(arrays["normals"]) != count:
			raise ValueError("positions, colors and normals must have the same length")
		if not np.isfinite(arrays["positions"]).all() or not np.isfinite(arrays["normals"]).all():
			raise ValueError("positions and normals must be finite")
		if int(arrays["triangles"].max()) >= count:
			raise ValueError("a triangle refers to a vertex that does not exist")
	except ValueError as error:
		raise ValueError(f"{path}: {error}") from error
	return arrays


def reference_payload(arrays) -> dict:
	"""The static reference mesh of a replay bundle: positions on a grid of their own (the Hydra snapshots have another one),
	normals as int16, 32-bit triangle indices only where 16 bits do not reach, everything deflated in one chunk."""
	positions, quant = quantize_positions(arrays["positions"])
	count = len(positions)
	packed = pack({
		"mesh_pos": positions,
		"mesh_col": arrays["colors"],
		"mesh_nrm": np.clip(np.round(arrays["normals"].astype(np.float64) * 32767.0), -32767, 32767).astype(np.int16),
		"mesh_faces": arrays["triangles"].astype(np.uint16) if count < 65536 else arrays["triangles"],
	})
	chunk = zlib.compress(packed, 6)
	return {
		"quant": quant,
		"chunk": base64.b64encode(chunk).decode(),
		"vertices": count,
		"triangles": len(arrays["triangles"]),
		"payload_bytes": len(chunk),
		"label": REFERENCE_LABEL,
	}


def merge_vertices(pos, col, faces, cell):
	"""Merge vertices that fall into the same cell (the mesh holds about 20 % coincident vertices) to shrink the bundle."""
	if cell <= 0 or len(pos) == 0:
		return pos, col, faces
	key = np.floor((pos - pos.min(axis=0)) / cell).astype(np.int64)
	span = key.max(axis=0) + 1
	_, first, inverse = np.unique(key[:, 0] + span[0] * (key[:, 1] + span[1] * key[:, 2]), return_index=True, return_inverse=True)
	faces = inverse[faces]
	faces = faces[(faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])]
	if len(faces):
		_, keep = np.unique(np.sort(faces, axis=1), axis=0, return_index=True)
		faces = faces[np.sort(keep)]  # duplicates dropped, the winding order of the others untouched
	return pos[first], col[first], faces.astype(np.uint32)


def find_run_dir(path: Path) -> Path:
	"""The folder with corrections.yaml: the run directory itself or its out_* child."""
	if (path / "corrections.yaml").exists():
		return path
	for child in sorted(path.glob("out_*")):
		if (child / "corrections.yaml").exists():
			return child
	return path


def label_names(run_dir) -> dict:
	"""Semantic label -> DAM description of the finished run (empty when there is none)."""
	if run_dir is None:
		return {}
	import yaml

	file = find_run_dir(Path(run_dir)) / "corrections.yaml"
	if not file.exists():
		return {}
	entries = (yaml.safe_load(file.read_text()) or {}).get("label_names", [])
	return {str(e["label"]): str(e["name"]) for e in entries if e.get("name") not in (None, "", "unknown")}


def description_sources(run_dir) -> dict:
	"""Semantic label -> exact pipeline frame selected as the input to DAM."""
	if run_dir is None:
		return {}
	import yaml

	file = find_run_dir(Path(run_dir)) / "corrections.yaml"
	if not file.exists():
		return {}
	entries = (yaml.safe_load(file.read_text()) or {}).get("label_names", [])
	sources = {}
	for entry in entries:
		source = entry.get("description_source") or {}
		try:
			frame_id = int(source["frame_id"])
		except (KeyError, TypeError, ValueError):
			continue
		if frame_id < 1:
			continue
		timestamp = source.get("timestamp")
		sources[str(entry["label"])] = {
			"frame_id": frame_id,
			"timestamp": float(timestamp) if timestamp is not None else None,
		}
	return sources


def estimate_times(numbers, snaps, fps) -> list:
	"""Data time (s) of each snapshot: the newest place update time, extrapolated linearly where there are no places yet.
	The pipeline publishes about one graph per frame, so the graph number is a good clock."""
	stamps = []
	for snap in snaps:
		values = [float(snap[key].max()) for key in ("place_t", "agent_t") if key in snap and len(snap[key])]
		stamps.append(max(values) if values else None)
	known = [(n, t) for n, t in zip(numbers, stamps) if t is not None]
	if len(known) >= 2:
		slope, intercept = np.polyfit([k[0] for k in known], [k[1] for k in known], 1)
		slope = max(slope, 1e-6)
	else:
		slope, intercept = 1.0 / fps, -1.0 / fps
	return [t if t is not None else max(0.0, intercept + slope * n) for n, t in zip(numbers, stamps)]


def load_snapshots(recording):
	"""Load the compact scene-graph snapshots written by ``record``."""
	rec = Path(recording)
	files = sorted(rec.glob("snap_*.npz"))
	if not files:
		return [], []
	numbers = [int(file.stem.split("_")[1]) for file in files]
	snaps = []
	for file in files:
		with np.load(file) as data:
			snaps.append({key: data[key] for key in data.files})
	return numbers, snaps


LEGACY_PLACE_MATCH_DISTANCE = 0.12


def ensure_entity_ids(snaps) -> None:
	"""Reconstruct persistent Place IDs in recordings made before category IDs were captured.

	Place nodes are stationary in Hydra's graph. Consecutive snapshots are matched conservatively by position; unmatched
	nodes receive monotonically increasing synthetic IDs. This survives array reordering and graph growth without merging
	distinct nearby Places into one history.
	"""
	previous_ids = np.zeros(0, np.int32)
	previous_pos = np.zeros((0, 3), np.float32)
	next_id = 0
	for snap in snaps:
		positions = np.asarray(snap.get("place_pos", np.zeros((0, 3), np.float32)), np.float32).reshape(-1, 3)
		if "place_cid" in snap:
			ids = np.asarray(snap["place_cid"], np.int32)
			if len(ids):
				next_id = max(next_id, int(ids.max()) + 1)
		else:
			ids = np.full(len(positions), -1, np.int32)
			if len(previous_pos) and len(positions):
				distances = np.linalg.norm(previous_pos[:, None, :] - positions[None, :, :], axis=2)
				candidates = sorted(
					(float(distances[old, new]), old, new)
					for old in range(len(previous_pos))
					for new in range(len(positions))
					if distances[old, new] <= LEGACY_PLACE_MATCH_DISTANCE
				)
				used_old, used_new = set(), set()
				for _, old, new in candidates:
					if old not in used_old and new not in used_new:
						ids[new] = previous_ids[old]
						used_old.add(old)
						used_new.add(new)
			for i in range(len(ids)):
				if ids[i] < 0:
					ids[i] = next_id
					next_id += 1
			snap["place_cid"] = ids
		previous_ids = ids.copy()
		previous_pos = positions.copy()


def entity_catalog(snaps, times) -> dict:
	"""Summarise every object and place over the retained snapshots for the viewer's entity browser."""
	objects, places = {}, {}
	for snap_index, (snap, timestamp) in enumerate(zip(snaps, times)):
		for i, category_id in enumerate(snap["obj_cid"]):
			key = int(category_id)
			center = (snap["obj_min"][i].astype(np.float64) + snap["obj_max"][i].astype(np.float64)) / 2.0
			item = objects.setdefault(key, {
				"id": key,
				"label": int(snap["obj_label"][i]),
				"first": round(float(timestamp), 3),
				"last": round(float(timestamp), 3),
				"count": 0,
				"snapshots": [],
				"first_center": center,
			})
			item["last"] = round(float(timestamp), 3)
			item["count"] += 1
			item["snapshots"].append(snap_index)
			item["last_center"] = center
		for category_id in snap["place_cid"]:
			key = int(category_id)
			item = places.setdefault(key, {
				"id": key,
				"first": round(float(timestamp), 3),
				"last": round(float(timestamp), 3),
				"count": 0,
				"snapshots": [],
			})
			item["last"] = round(float(timestamp), 3)
			item["count"] += 1
			item["snapshots"].append(snap_index)

	object_items = []
	for item in objects.values():
		motion = float(np.linalg.norm(item.pop("last_center") - item.pop("first_center")))
		item["motion"] = round(motion, 3)
		object_items.append(item)
	return {
		"objects": sorted(object_items, key=lambda item: item["id"]),
		"places": sorted(places.values(), key=lambda item: item["id"]),
	}


def edge_strips(first, second, pairs):
	"""Return valid two-point line strips for index pairs into two position arrays."""
	return [
		np.stack((first[int(a)], second[int(b)]))
		for a, b in pairs
		if 0 <= int(a) < len(first) and 0 <= int(b) < len(second)
	]


def log_rrd_snapshot(rr, snap, object_names):
	"""Replace the dynamic map entities with one complete scene-graph snapshot."""
	for path in ("world/mesh", "world/dsg", "world/camera"):
		rr.log(path, rr.Clear(recursive=True))

	mesh_pos, mesh_col, mesh_faces = snap["mesh_pos"], snap["mesh_col"], snap["mesh_faces"]
	if len(mesh_pos) and len(mesh_faces):
		rr.log(
			"world/mesh",
			rr.Mesh3D(
				vertex_positions=mesh_pos,
				vertex_colors=mesh_col,
				triangle_indices=mesh_faces,
			),
		)

	obj_min, obj_max = snap["obj_min"], snap["obj_max"]
	obj_centers = (obj_min + obj_max) * 0.5
	if len(obj_centers):
		labels = []
		for category_id, semantic_label in zip(snap["obj_cid"], snap["obj_label"]):
			name = object_names.get(str(int(semantic_label)), f"label {int(semantic_label)}")
			labels.append(f"O{int(category_id)}: {name}")
		rr.log(
			"world/dsg/objects",
			rr.Boxes3D(
				centers=obj_centers,
				sizes=obj_max - obj_min,
				colors=[230, 70, 70],
				labels=labels,
			),
		)

	place_pos = snap["place_pos"]
	if len(place_pos):
		place_radii = np.clip(snap["place_rad"] * 0.15, 0.03, 0.15)
		rr.log("world/dsg/places", rr.Points3D(place_pos, radii=place_radii, colors=[70, 210, 90]))
		strips = edge_strips(place_pos, place_pos, snap["place_edges"])
		if strips:
			rr.log("world/dsg/edges/place_place", rr.LineStrips3D(strips, colors=[70, 180, 90]))

	room_pos = snap["room_pos"]
	if len(room_pos):
		rr.log("world/dsg/rooms", rr.Points3D(room_pos, radii=0.18, colors=[70, 120, 255], labels=[f"R{i}" for i in range(len(room_pos))]))
		strips = edge_strips(room_pos, place_pos, snap["room_place_edges"])
		if strips:
			rr.log("world/dsg/edges/room_place", rr.LineStrips3D(strips, colors=[90, 140, 255]))

	bld_pos = snap["bld_pos"]
	if len(bld_pos):
		rr.log("world/dsg/buildings", rr.Points3D(bld_pos, radii=0.24, colors=[245, 210, 70], labels=[f"B{i}" for i in range(len(bld_pos))]))
		strips = edge_strips(bld_pos, room_pos, snap["bld_room_edges"])
		if strips:
			rr.log("world/dsg/edges/building_room", rr.LineStrips3D(strips, colors=[230, 190, 70]))

	strips = edge_strips(place_pos, obj_centers, snap["obj_place_edges"])
	if strips:
		rr.log("world/dsg/edges/place_object", rr.LineStrips3D(strips, colors=[170, 170, 170]))

	agent_pos = snap["agent_pos"]
	if len(agent_pos):
		rr.log("world/camera/trajectory", rr.LineStrips3D([agent_pos], colors=[230, 70, 230]))
		quaternion = snap["agent_quat"][-1]
		rr.log(
			"world/camera/current",
			rr.Transform3D(
				translation=agent_pos[-1],
				quaternion=[quaternion[1], quaternion[2], quaternion[3], quaternion[0]],
			),
		)
		rr.log(
			"world/camera/current/forward",
			rr.Arrows3D(
				origins=[[0.0, 0.0, 0.0]],
				vectors=[[0.0, 0.0, 0.35]],
				radii=0.015,
				colors=[230, 70, 230],
			),
		)


def rrd(args) -> int:
	"""Write recorded scene-graph snapshots to a timed native Rerun recording."""
	import rerun as rr

	numbers, snaps = load_snapshots(args.recording)
	if not snaps:
		print(f"[replay] no snapshots in {args.recording}: cannot build a Rerun recording", file=sys.stderr)
		return 1

	times = estimate_times(numbers, snaps, args.fps)
	object_names = label_names(find_run_dir(Path(args.run))) if args.run else {}
	out = Path(args.out)
	out.parent.mkdir(parents=True, exist_ok=True)
	rr.init("daaam_replay", spawn=False)
	rr.save(out)
	rr.log("world", rr.ViewCoordinates.RIGHT_HAND_Z_UP, static=True)

	for number, time_s, snap in zip(numbers, times, snaps):
		snap["mesh_pos"], snap["mesh_col"], snap["mesh_faces"] = merge_vertices(
			snap["mesh_pos"], snap["mesh_col"], snap["mesh_faces"], args.merge
		)
		rr.set_time("frame", sequence=number)
		rr.set_time("time", duration=time_s)
		log_rrd_snapshot(rr, snap, object_names)

	rr.disconnect()
	print(f"[replay] {len(snaps)} timed snapshots -> {out} ({out.stat().st_size / 1e6:.1f} MB)")
	return 0


def finalize(args) -> int:
	"""Turn a recording, or the final scene graph of a run without one, into the run's versioned replay/ bundle."""
	if (args.recording is None) == (args.graph is None):
		args.parser.error("finalize needs exactly one source: a recording directory or --graph DSG")
	sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
	from scripts.replay_bundle import BundleOptions, describe, finalize_bundle

	options = BundleOptions(
		recording=Path(args.recording) if args.recording else None,
		source_graph=Path(args.graph) if args.graph else None,
		run_dir=Path(args.run),
		output=Path(args.out),
		dataset_kind=args.dataset_kind,
		sequence=args.sequence,
		dataset=Path(args.dataset) if args.dataset else None,
		fps=args.fps,
		time_origin=args.time_origin,
		start_frame=args.start_frame,
		frame_count=args.frame_count,
		title=args.title or "",
		merge=args.merge,
		thumb_width=args.thumb_width,
		max_depth=args.max_depth,
		reference_mesh=Path(args.reference_mesh) if args.reference_mesh else None,
		max_chunk_snapshots=args.max_chunk_snapshots,
		max_chunk_bytes=args.max_chunk_bytes,
	)
	try:
		manifest_path = finalize_bundle(options)
	except (ValueError, OSError) as error:
		print(f"[replay] finalize failed: {error}", file=sys.stderr)
		return 1
	manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
	reference = manifest.get("reference_mesh")
	if reference:
		print(f"[replay] reference mesh: {reference['vertices']:,} vertices, {reference['triangles']:,} triangles, payload {reference['bytes'] / 1e6:.1f} MB")
	print(describe(manifest, manifest_path.parent))
	return 0


def main() -> int:
	parser = argparse.ArgumentParser(description="Record a mapping run and finalize its replay data bundle (opened with just replay-web)")
	sub = parser.add_subparsers(dest="command", required=True)

	p = sub.add_parser("record", help="subscribe to Hydra's ZMQ stream and save snapshots")
	p.add_argument("--out", required=True, help="directory for the snapshots")
	p.add_argument("--zmq-url", default=DEFAULT_URL)
	p.add_argument("--every", type=int, default=5, help="keep every n-th graph (about one is published per frame)")
	p.set_defaults(func=record)

	p = sub.add_parser("finalize", help="turn a recording (or a final scene graph) into the versioned replay/ data bundle of a run")
	p.add_argument("recording", nargs="?", help="directory written by `record`; leave out and give --graph for a one-snapshot bundle")
	p.add_argument("--graph", help="final scene graph (dsg.json) of a run without a recording, e.g. CODa")
	p.add_argument("--run", required=True, help="pipeline output directory of the run (object descriptions are read from its corrections.yaml)")
	p.add_argument("--out", required=True, help="bundle directory to publish, normally RUN/replay")
	p.add_argument("--dataset-kind", choices=("tum", "coda"), required=True)
	p.add_argument("--sequence", required=True, help="dataset sequence: a TUM name such as fr1_desk, or a CODa sequence number")
	p.add_argument("--dataset", help="dataset directory: camera images of every snapshot and the exact DAM source frames are copied from it")
	p.add_argument("--fps", type=float, default=15.0, help="frame rate of the run (run time -> dataset frame)")
	p.add_argument("--time-origin", type=float, help="dataset time of t = 0 (default: TUM 0, CODa the first line of timestamps/<sequence>.txt)")
	p.add_argument("--start-frame", type=int, default=0, help="first dataset frame of the run (CODa bags can start later)")
	p.add_argument("--frame-count", type=int, help="number of frames in the run (required with --graph)")
	p.add_argument("--title")
	p.add_argument("--merge", type=float, default=0.005, help="merge mesh vertices closer than this many metres (0 = off)")
	p.add_argument("--thumb-width", type=int, default=320, help="width in pixels of the camera images")
	p.add_argument("--max-depth", type=float, help="depth in metres at the far end of the colour scale (default: TUM 4, CODa 30)")
	p.add_argument("--reference-mesh", help="cache (.npz) of the 1 cm reference mesh written by scripts/build_reference_mesh.py")
	p.add_argument("--max-chunk-snapshots", type=int, default=24, help="at most this many snapshots per timeline chunk")
	p.add_argument("--max-chunk-bytes", type=int, default=8 * 1024 * 1024, help="uncompressed size target of a timeline chunk")
	p.set_defaults(func=finalize, parser=p)

	p = sub.add_parser("rrd", help="turn a recording into a timed native Rerun .rrd file")
	p.add_argument("recording", help="directory written by `record`")
	p.add_argument("--run", help="pipeline output directory of the run (object descriptions are read from its corrections.yaml)")
	p.add_argument("--out", default="replay.rrd")
	p.add_argument("--merge", type=float, default=0.005, help="merge mesh vertices closer than this many metres (0 = off)")
	p.add_argument("--fps", type=float, default=15.0, help="frame rate used when a snapshot has no graph timestamp")
	p.set_defaults(func=rrd)

	args = parser.parse_args()
	return args.func(args)


if __name__ == "__main__":
	sys.exit(main())
