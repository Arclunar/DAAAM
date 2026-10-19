"""Convert a ROS1 bag of a RealSense D455 (colour + aligned depth + camera info) plus poses into a posed RGB-D folder.

	just gs-bag /path/to/recording.bag --list                     the bag's topics, types, counts and rates
	just gs-bag /path/to/recording.bag --extrinsic /calib/chain.yaml:T_cam_livox

(in the container: python -m scripts.gs_world.rosbag_to_rgbd BAG [OUT] [options]). The output folder is the layout
gs_world.train and DAAAM read; default /data/gs_world/datasets/<bag name>.

Inputs, with realsense-ros (align_depth) defaults: colour /camera/color/image_raw or .../compressed, depth
/camera/aligned_depth_to_color/image_raw or .../compressedDepth (16UC1 in --depth-unit metres, or 32FC1 metres), and
/camera/color/camera_info. Poses come from a topic in the bag (nav_msgs/Odometry such as FAST-LIO's /Odometry,
geometry_msgs/PoseStamped or PoseWithCovarianceStamped) or from a TUM trajectory file (--trajectory, e.g. FAST-LIO run
offline). They describe a tracked frame (the LiDAR IMU for FAST-LIO); --extrinsic gives the colour camera's optical
frame in it, unless the poses already are camera poses (--pose-is-camera, or the pose topic's child frame is the
camera frame).

Processing: colour and depth are paired by header stamp (--max-dt); the pose is interpolated at the colour stamp plus
--time-offset; a keyframe is kept every --min-translation metres or --min-rotation degrees of motion, choosing the
sharpest image of each stretch (variance of the Laplacian); colour and depth are undistorted with the same map so the
output is pinhole (camera_info distortion, or a Kalibr calibration with --camchain). conversion.json records the
inputs, the calibration used and the statistics.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from .data import Camera, LayoutWriter, read_trajectory
from .geometry import interpolate_poses, invert, is_rigid, path_length, rotation_angle_deg
from .rosbag1 import (POSE_PARSERS, Bag, BagError, CameraInfo, decode_compressed_color, decode_compressed_depth,
	header_stamp, image_to_array, parse_camera_info, parse_compressed_image, parse_image)

OUTPUT_ROOT = Path(os.environ.get("GS_OUTPUT_ROOT", "/data/gs_world"))
RGB_TOPICS = ("/camera/color/image_raw", "/camera/color/image_raw/compressed")
DEPTH_TOPICS = ("/camera/aligned_depth_to_color/image_raw", "/camera/aligned_depth_to_color/image_raw/compressedDepth")
INFO_TOPICS = ("/camera/color/camera_info",)
IMAGE_TYPES = ("sensor_msgs/Image", "sensor_msgs/CompressedImage")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
	p = argparse.ArgumentParser(description="Convert a ROS1 D455 bag plus poses into a posed RGB-D folder.",
		formatter_class=argparse.ArgumentDefaultsHelpFormatter)
	p.add_argument("bag", type=Path, help="ROS1 bag file")
	p.add_argument("out", type=Path, nargs="?", default=None, help="output folder (default: /data/gs_world/datasets/<bag name>)")
	p.add_argument("--list", action="store_true", help="only list the bag's topics")
	p.add_argument("--overwrite", action="store_true")
	p.add_argument("--rgb-topic", default=None, help=f"colour topic (default: the first of {', '.join(RGB_TOPICS)} in the bag)")
	p.add_argument("--depth-topic", default=None, help=f"aligned depth topic (default: the first of {', '.join(DEPTH_TOPICS)})")
	p.add_argument("--info-topic", default=None, help=f"colour CameraInfo topic (default: {INFO_TOPICS[0]})")
	p.add_argument("--pose-topic", default=None, help="pose topic (default: the bag's only Odometry, else only PoseStamped topic)")
	p.add_argument("--trajectory", type=Path, default=None, help="TUM trajectory file (time tx ty tz qx qy qz qw) instead of a pose topic")
	p.add_argument("--extrinsic", default=None,
		help="FILE[:KEY] with the 4x4 camera extrinsic; keys follow p_a = T_a_b p_b, T_cam_<x> is inverted (chain.yaml:T_cam_livox)")
	p.add_argument("--pose-is-camera", action="store_true", help="the poses already are poses of the colour camera's optical frame")
	p.add_argument("--camchain", default=None, help="Kalibr camchain FILE[:camN] whose intrinsics/distortion replace camera_info")
	p.add_argument("--time-offset", type=float, default=0.0, help="seconds added to image stamps before the pose lookup")
	p.add_argument("--max-dt", type=float, default=0.02, help="max colour/depth stamp difference (s)")
	p.add_argument("--max-pose-gap", type=float, default=0.25, help="max gap between the poses bracketing an image (s)")
	p.add_argument("--min-translation", type=float, default=0.05, help="keyframe spacing in metres")
	p.add_argument("--min-rotation", type=float, default=5.0, help="keyframe spacing in degrees")
	p.add_argument("--max-frames", type=int, default=1000, help="keep at most this many keyframes (evenly thinned)")
	p.add_argument("--start", type=float, default=0.0, help="skip this many seconds after the first image")
	p.add_argument("--duration", type=float, default=0.0, help="use only this many seconds (0: to the end)")
	p.add_argument("--depth-unit", type=float, default=0.001, help="metres per unit of 16-bit depth (RealSense: 0.001)")
	p.add_argument("--max-depth", type=float, default=8.0, help="drop depth beyond this distance (m)")
	return p.parse_args(argv)


def _matrix_entries(data) -> Dict[str, np.ndarray]:
	found = {}
	if isinstance(data, dict):
		for key, value in data.items():
			try:
				matrix = np.asarray(value, dtype=np.float64)
			except (TypeError, ValueError):
				continue
			if matrix.size == 16:
				found[str(key)] = matrix.reshape(4, 4)
	return found


def load_extrinsic(spec: str) -> Tuple[np.ndarray, str]:
	"""T_tracking_camera (maps camera points into the tracked frame) from FILE[:KEY].

	FILE is YAML or JSON with 4x4 matrices (KEY picks one; without KEY the file must hold exactly one, or a
	'T_tracking_camera' entry), or a text file with 16 numbers. Matrix names follow p_a = T_a_b @ p_b, so a key
	'T_cam_<x>' (as in camera_info/D455_rgb_cali/chain.yaml) maps <x> into the camera and is inverted.
	"""
	path_text, key = spec, ""
	match = re.match(r"^(.*):([A-Za-z_][A-Za-z0-9_]*)$", spec)
	if match and not Path(spec).exists():
		path_text, key = match.group(1), match.group(2)
	path = Path(path_text)
	text = path.read_text(encoding="utf-8")
	if path.suffix.lower() in (".yaml", ".yml", ".json"):
		if path.suffix.lower() == ".json":
			data = json.loads(text)
		else:
			import yaml

			data = yaml.safe_load(text)
		entries = _matrix_entries(data)
		if key:
			if key not in entries:
				raise SystemExit(f"{path} has no 4x4 matrix '{key}'; matrices in it: {', '.join(entries) or 'none'}")
			name, matrix = key, entries[key]
		elif "T_tracking_camera" in entries:
			name, matrix = "T_tracking_camera", entries["T_tracking_camera"]
		elif len(entries) == 1:
			name, matrix = next(iter(entries.items()))
		else:
			raise SystemExit(f"{path} holds {len(entries)} 4x4 matrices ({', '.join(entries)}); pick one with {path}:KEY")
	else:
		values = [float(v) for v in text.replace(",", " ").split()]
		if len(values) != 16:
			raise SystemExit(f"{path}: expected 16 numbers (a row-major 4x4 matrix), got {len(values)}")
		name, matrix = path.name, np.asarray(values).reshape(4, 4)
	if not is_rigid(matrix, tol=1e-3):
		raise SystemExit(f"{spec}: the matrix is not a rigid transform")
	if re.match(r"^T_cam(era)?_", name, re.IGNORECASE):
		return invert(matrix), f"{path}:{name} (inverted: {name} maps into the camera)"
	return matrix, f"{path}:{name}"


def load_camchain(spec: str) -> Tuple[np.ndarray, np.ndarray, str, Tuple[int, int]]:
	"""(K, distortion, model, (width, height)) of one camera of a Kalibr camchain YAML ('FILE[:camN]', default cam0)."""
	import yaml

	path_text, cam = spec, "cam0"
	match = re.match(r"^(.*):(cam\d+)$", spec)
	if match:
		path_text, cam = match.group(1), match.group(2)
	data = yaml.safe_load(Path(path_text).read_text(encoding="utf-8"))
	if cam not in data:
		raise SystemExit(f"{path_text} has no {cam}")
	entry = data[cam]
	if entry.get("camera_model", "pinhole") != "pinhole":
		raise SystemExit(f"{cam}: camera model {entry.get('camera_model')!r} is not supported (pinhole only)")
	fx, fy, cx, cy = [float(v) for v in entry["intrinsics"]]
	K = np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]])
	model = {"radtan": "plumb_bob", "equidistant": "equidistant"}.get(entry.get("distortion_model", "radtan"))
	if model is None:
		raise SystemExit(f"{cam}: distortion model {entry.get('distortion_model')!r} is not supported")
	width, height = [int(v) for v in entry["resolution"]]
	return K, np.asarray(entry.get("distortion_coeffs", []), dtype=np.float64), model, (width, height)


def undistortion(K: np.ndarray, D: np.ndarray, model: str, size: Tuple[int, int]):
	"""(map1, map2, valid mask) that undistort to a pinhole image with the same K, or None without distortion."""
	if not np.any(np.abs(D) > 1e-12):
		return None
	width, height = size
	model = (model or "plumb_bob").lower()
	if model in ("plumb_bob", "rational_polynomial", "radtan"):
		map1, map2 = cv2.initUndistortRectifyMap(K, D, None, K, (width, height), cv2.CV_32FC1)
	elif model in ("equidistant", "fisheye", "kannala_brandt"):
		map1, map2 = cv2.fisheye.initUndistortRectifyMap(K, D[:4], np.eye(3), K, (width, height), cv2.CV_32FC1)
	else:
		raise SystemExit(f"distortion model {model!r} is not supported")
	valid = cv2.remap(np.full((height, width), 255, np.uint8), map1, map2, cv2.INTER_NEAREST,
		borderMode=cv2.BORDER_CONSTANT, borderValue=0) > 0
	valid = cv2.erode(valid.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
	return map1, map2, valid


def select_keyframes(poses: np.ndarray, scores: Sequence[float], min_translation: float,
		min_rotation_deg: float) -> List[int]:
	"""Indices of keyframes: the sharpest image of each stretch of motion.

	The trajectory is cut into consecutive stretches that end where a frame is min_translation metres or
	min_rotation_deg degrees away from the stretch's first frame. From each stretch the frame with the highest score
	is kept, among those at least half that spacing away from the previous keyframe (no near-duplicates).
	"""
	def apart(a: np.ndarray, b: np.ndarray, factor: float = 1.0) -> bool:
		return (np.linalg.norm(a[:3, 3] - b[:3, 3]) >= factor * min_translation
			or rotation_angle_deg(a[:3, :3].T @ b[:3, :3]) >= factor * min_rotation_deg)

	keep: List[int] = []
	start, n = 0, len(poses)
	while start < n:
		end = start + 1
		while end < n and not apart(poses[start], poses[end]):
			end += 1
		stretch = range(start, end)
		eligible = [j for j in stretch if not keep or apart(poses[keep[-1]], poses[j], 0.5)]
		if eligible:
			keep.append(max(eligible, key=lambda j: scores[j]))
		start = end
	return keep


def sharpness(gray: np.ndarray) -> float:
	return float(cv2.Laplacian(gray, cv2.CV_32F).var())


def pick_topic(requested: Optional[str], candidates: Sequence[str], topics: Dict, types: Sequence[str], what: str) -> str:
	if requested:
		if requested not in topics:
			raise SystemExit(f"{what} topic {requested} is not in the bag (see --list)")
		chosen = requested
	else:
		chosen = next((t for t in candidates if t in topics), None)
		if chosen is None:
			raise SystemExit(f"no {what} topic found (looked for {', '.join(candidates)}); pass it explicitly (see --list)")
	if topics[chosen].type not in types:
		raise SystemExit(f"{what} topic {chosen} has type {topics[chosen].type}, expected {' or '.join(types)}")
	return chosen


def print_topics(bag: Bag, topics: Dict) -> None:
	print(f"{bag.path} ({bag.size / 2 ** 30:.2f} GiB){'  [cut off: no index]' if bag.truncated else ''}")
	width = max([len(t) for t in topics] + [5])
	for topic in sorted(topics):
		info = topics[topic]
		span = ((info.last_ns - info.first_ns) / 1e9) if info.first_ns is not None and info.last_ns is not None else 0.0
		rate = (info.count - 1) / span if span > 0 and info.count > 1 else 0.0
		print(f"  {topic:<{width}}  {info.type:<40}  {info.count:7d} msgs  {rate:6.1f} Hz  {span:7.1f} s")


def main(argv: Optional[Sequence[str]] = None) -> int:
	args = parse_args(argv)
	try:
		return convert(args)
	except BagError as exc:
		print(f"error: {exc}", file=sys.stderr)
		return 1


def convert(args: argparse.Namespace) -> int:
	bag = Bag(args.bag)
	topics = bag.topics()
	if args.list:
		print_topics(bag, topics)
		return 0
	rgb_topic = pick_topic(args.rgb_topic, RGB_TOPICS, topics, IMAGE_TYPES, "colour")
	depth_topic = pick_topic(args.depth_topic, DEPTH_TOPICS, topics, IMAGE_TYPES, "depth")
	info_topic = pick_topic(args.info_topic, INFO_TOPICS, topics, ("sensor_msgs/CameraInfo",), "camera info")
	pose_topic = None
	if args.trajectory is None:
		if args.pose_topic:
			pose_topic = pick_topic(args.pose_topic, (), topics, tuple(POSE_PARSERS), "pose")
		else:
			for kind in ("nav_msgs/Odometry", "geometry_msgs/PoseStamped", "geometry_msgs/PoseWithCovarianceStamped"):
				found = sorted(t for t, info in topics.items() if info.type == kind)
				if len(found) > 1:
					raise SystemExit(f"several {kind} topics ({', '.join(found)}): choose one with --pose-topic")
				if found:
					pose_topic = found[0]
					break
			if pose_topic is None:
				raise SystemExit("the bag has no pose topic (Odometry / PoseStamped); pass --trajectory with a TUM "
					"trajectory, e.g. from running FAST-LIO on the bag")
	out = args.out or OUTPUT_ROOT / "datasets" / args.bag.stem
	if out.exists() and not args.overwrite:
		raise SystemExit(f"{out} already exists; choose another output or pass --overwrite")
	print(f"colour {rgb_topic}, depth {depth_topic}, camera info {info_topic}, poses "
		f"{pose_topic or args.trajectory}")

	# pass 1: stamps, sharpness, calibration and poses (only the small messages are kept)
	rgb_meta: List[Tuple[int, float]] = []
	depth_stamps: List[int] = []
	info: Optional[CameraInfo] = None
	samples = []
	frames = {"rgb": "", "depth": ""}
	rgb_compressed = topics[rgb_topic].type == "sensor_msgs/CompressedImage"
	wanted = [rgb_topic, depth_topic, info_topic] + ([pose_topic] if pose_topic else [])
	for msg in bag.messages(wanted):
		if msg.topic == rgb_topic:
			if rgb_compressed:
				image = parse_compressed_image(msg.data)
				gray = decode_compressed_color(image, cv2.IMREAD_REDUCED_GRAYSCALE_4)
			else:
				image = parse_image(msg.data)
				pixels = image_to_array(image)
				gray = cv2.cvtColor(pixels[::4, ::4], cv2.COLOR_RGB2GRAY) if pixels.ndim == 3 else pixels[::4, ::4]
			frames["rgb"] = frames["rgb"] or image.frame_id
			rgb_meta.append((image.stamp_ns, sharpness(gray)))
		elif msg.topic == depth_topic:
			depth_stamps.append(header_stamp(msg.data))
			if not frames["depth"]:
				parsed = parse_compressed_image(msg.data) if topics[depth_topic].type.endswith("CompressedImage") else parse_image(msg.data)
				frames["depth"] = parsed.frame_id
		elif msg.topic == info_topic:
			if info is None:
				info = parse_camera_info(msg.data)
		else:
			samples.append(POSE_PARSERS[msg.type](msg.data))
	if bag.truncated:
		print("warning: the bag is cut off (interrupted recording or copy); using the messages before the cut")
	if info is None or not rgb_meta or not depth_stamps:
		raise SystemExit(f"the bag needs colour, depth and camera info messages: {len(rgb_meta)} colour, "
			f"{len(depth_stamps)} depth, {'one' if info else 'no'} camera info")
	print(f"pass 1: {len(rgb_meta)} colour, {len(depth_stamps)} depth images, "
		f"{len(samples) if pose_topic else 'file'} poses; camera {info.width}x{info.height} '{info.distortion_model}'")

	# calibration and extrinsic
	K, D, model, size = info.K, info.D, info.distortion_model, (info.width, info.height)
	calibration = f"camera_info on {info_topic}"
	if args.camchain:
		K, D, model, size = load_camchain(args.camchain)
		if size != (info.width, info.height):
			raise SystemExit(f"{args.camchain} is for {size[0]}x{size[1]} images, the bag has {info.width}x{info.height}")
		calibration = f"Kalibr {args.camchain}"
	child_frame = samples[0].child_frame_id if samples else ""
	if args.pose_is_camera:
		T_tracking_camera, extrinsic_note = np.eye(4), "identity (--pose-is-camera)"
	elif args.extrinsic:
		T_tracking_camera, extrinsic_note = load_extrinsic(args.extrinsic)
	elif child_frame and child_frame == frames["rgb"]:
		T_tracking_camera, extrinsic_note = np.eye(4), f"identity (poses are of {child_frame})"
	else:
		raise SystemExit(f"the poses describe '{child_frame or 'an unnamed frame'}', the images come from "
			f"'{frames['rgb']}': pass --extrinsic FILE[:KEY] (e.g. chain.yaml:T_cam_livox) or --pose-is-camera")

	# pose samples
	if pose_topic:
		sample_t = np.array([s.stamp_ns for s in samples], dtype=np.float64) / 1e9
		sample_T = np.array([s.T for s in samples])
	else:
		sample_T, times = read_trajectory(args.trajectory)
		if times is None:
			raise SystemExit(f"{args.trajectory} has no timestamps: use the TUM format 'time tx ty tz qx qy qz qw'")
		sample_t = np.asarray(times, dtype=np.float64)
	if len(sample_t) < 2:
		raise SystemExit("fewer than two poses")

	# pair colour with depth, restrict to the time window, look up the poses
	rgb_t = np.array([m[0] for m in rgb_meta], dtype=np.int64)
	depth_t = np.array(depth_stamps, dtype=np.int64)
	depth_order = np.argsort(depth_t, kind="stable")
	sorted_depth = depth_t[depth_order]
	pos = np.clip(np.searchsorted(sorted_depth, rgb_t), 1, len(sorted_depth) - 1) if len(sorted_depth) > 1 else np.zeros(len(rgb_t), int)
	left = sorted_depth[np.maximum(pos - 1, 0)]
	right = sorted_depth[pos]
	nearest = np.where(np.abs(rgb_t - left) <= np.abs(right - rgb_t), np.maximum(pos - 1, 0), pos)
	skew_s = (sorted_depth[nearest] - rgb_t) / 1e9
	paired = np.abs(skew_s) <= args.max_dt
	t0 = rgb_t.min() / 1e9
	in_window = rgb_t / 1e9 >= t0 + args.start
	if args.duration > 0:
		in_window &= rgb_t / 1e9 <= t0 + args.start + args.duration
	query = rgb_t / 1e9 + args.time_offset
	T_world_tracking, has_pose = interpolate_poses(sample_t, sample_T, query, args.max_pose_gap)
	candidates = np.flatnonzero(paired & in_window & has_pose)
	candidates = candidates[np.argsort(rgb_t[candidates], kind="stable")]  # by header stamp, not arrival
	print(f"{int(paired.sum())} colour images have depth within {args.max_dt * 1000:.0f} ms, "
		f"{int((paired & in_window & has_pose).sum())} of them in the window with a pose")
	if len(candidates) == 0:
		raise SystemExit("no colour image has both depth and a pose; check --time-offset (pose and image clocks), "
			"--max-dt and the pose coverage")
	cam_poses = T_world_tracking[candidates] @ T_tracking_camera
	scores = [rgb_meta[i][1] for i in candidates]
	chosen = select_keyframes(cam_poses, scores, args.min_translation, args.min_rotation)
	if len(chosen) > args.max_frames:
		chosen = [chosen[i] for i in np.linspace(0, len(chosen) - 1, args.max_frames).round().astype(int)]
		print(f"thinned to {args.max_frames} keyframes (raise --min-translation / --min-rotation for fewer)")
	selected = [int(candidates[j]) for j in chosen]
	selected_poses = cam_poses[chosen]
	duration = (rgb_t[selected[-1]] - rgb_t[selected[0]]) / 1e9 if len(selected) > 1 else 0.0
	fps = round((len(selected) - 1) / duration, 3) if duration > 0 else 1.0
	print(f"{len(selected)} keyframes over {duration:.1f} s, path {path_length(selected_poses):.2f} m")

	# pass 2: decode the selected images, undistort, write
	rgb_wanted = {rgb_index: k for k, rgb_index in enumerate(selected)}
	depth_wanted: Dict[int, List[int]] = {}
	for k, rgb_index in enumerate(selected):
		depth_wanted.setdefault(int(depth_order[nearest[rgb_index]]), []).append(k)
	maps = undistortion(K, D, model, size)
	camera = Camera(size[0], size[1], np.asarray(K, dtype=np.float64))
	source = f"ROS1 bag {args.bag.name}: {rgb_topic} + {depth_topic}, poses {pose_topic or args.trajectory}"
	writer = LayoutWriter(out, camera, fps, source, overwrite=args.overwrite, extra_info={"converter": "gs_world.rosbag_to_rgbd"})
	colours: Dict[int, np.ndarray] = {}
	depths: Dict[int, np.ndarray] = {}
	written = 0
	counters = {rgb_topic: -1, depth_topic: -1}
	depth_compressed = topics[depth_topic].type == "sensor_msgs/CompressedImage"
	for msg in bag.messages([rgb_topic, depth_topic]):
		counters[msg.topic] += 1
		ordinal = counters[msg.topic]
		if msg.topic == rgb_topic and ordinal in rgb_wanted:
			rgb = decode_compressed_color(parse_compressed_image(msg.data)) if rgb_compressed else image_to_array(parse_image(msg.data))
			if rgb.ndim != 3 or rgb.shape[:2] != (size[1], size[0]):
				raise SystemExit(f"colour image is {rgb.shape[1]}x{rgb.shape[0]}, the calibration is for {size[0]}x{size[1]}")
			colours[rgb_wanted[ordinal]] = cv2.remap(rgb, maps[0], maps[1], cv2.INTER_LINEAR) if maps else rgb
		elif msg.topic == depth_topic and ordinal in depth_wanted:
			raw = decode_compressed_depth(parse_compressed_image(msg.data)) if depth_compressed else image_to_array(parse_image(msg.data))
			if raw.shape != (size[1], size[0]):
				raise SystemExit(f"depth image is {raw.shape[1]}x{raw.shape[0]}, colour is {size[0]}x{size[1]}: "
					"record the depth aligned to colour (aligned_depth_to_color)")
			metres = raw.astype(np.float32) * np.float32(args.depth_unit) if raw.dtype == np.uint16 else raw.astype(np.float32)
			metres[(metres > args.max_depth) | ~np.isfinite(metres)] = 0.0
			if maps:
				metres = cv2.remap(metres, maps[0], maps[1], cv2.INTER_NEAREST)
			for k in depth_wanted[ordinal]:
				depths[k] = metres
		while written in colours and written in depths:
			writer.add(colours.pop(written), depths.pop(written), selected_poses[written], rgb_t[selected[written]] / 1e9)
			written += 1
	if written < len(selected):
		raise SystemExit(f"only {written} of {len(selected)} keyframes could be decoded in the second pass")
	out = writer.close(valid_mask=maps[2] if maps else None)
	report = {
		"bag": str(args.bag),
		"bag_bytes": bag.size,
		"bag_truncated": bag.truncated,
		"topics": {"rgb": rgb_topic, "depth": depth_topic, "camera_info": info_topic, "pose": pose_topic},
		"trajectory_file": str(args.trajectory) if args.trajectory else None,
		"camera_frame": frames["rgb"],
		"depth_frame": frames["depth"],
		"pose_child_frame": child_frame,
		"calibration": calibration,
		"K": np.asarray(K).tolist(),
		"distortion": np.asarray(D).tolist(),
		"distortion_model": model,
		"undistorted": maps is not None,
		"T_tracking_camera": T_tracking_camera.tolist(),
		"extrinsic": extrinsic_note,
		"time_offset_s": args.time_offset,
		"depth_unit_m": args.depth_unit,
		"max_depth_m": args.max_depth,
		"counts": {"rgb": len(rgb_meta), "depth": len(depth_stamps), "poses": int(len(sample_t)),
			"paired": int(paired.sum()), "candidates": int(len(candidates)), "keyframes": len(selected)},
		"rgb_depth_skew_ms": {"median": float(np.median(np.abs(skew_s[candidates])) * 1000),
			"max": float(np.max(np.abs(skew_s[candidates])) * 1000)},
		"keyframe_spacing": {"min_translation_m": args.min_translation, "min_rotation_deg": args.min_rotation},
		"duration_s": round(float(duration), 3),
		"path_length_m": round(path_length(selected_poses), 3),
		"created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
	}
	(out / "conversion.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
	print(f"wrote {written} frames to {out} (extrinsic: {extrinsic_note}; calibration: {calibration}"
		f"{'; undistorted' if maps else ''})")
	print(f"train a model on it: just gs-train {out}")
	return 0


if __name__ == "__main__":
	sys.exit(main())
