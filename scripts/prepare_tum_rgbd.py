#!/usr/bin/env python3
"""Convert a TUM RGB-D sequence into the folder layout read by ImageSequenceDataset.

Usage:
	python scripts/prepare_tum_rgbd.py /datasets/raw/rgbd_dataset_freiburg1_desk /datasets/tum_fr1_desk --stride 2

Output layout (see src/daaam/datasets/loaders/image_sequence.py):
	output_dir/
		rgb/000000.png ...          undistorted 8-bit RGB
		depth/000000.png ...        undistorted 16-bit depth, 5000 units per metre
		pose/poses.txt              one row-major 4x4 T_world_camera (16 numbers) per frame
		camera_info.json            width, height, intrinsics (3x3), distortion (all zero after undistortion)

Run the pipeline on the result with:
	--dataset-type ImageSequenceDataset --depth-scale 5000 --fps <30 / stride>

Frames are kept only if a depth image exists within --max-dt seconds and the ground-truth
trajectory brackets the RGB timestamp; poses are interpolated (lerp + slerp) to the RGB time.
"""

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

# Kinect colour-camera calibration published with the dataset
# (https://cvg.cit.tum.de/data/datasets/rgbd-dataset/file_formats).
# Depth images are registered to the colour camera, so the same model applies to both.
INTRINSICS = {
	"freiburg1": dict(fx=517.3, fy=516.5, cx=318.6, cy=255.3, dist=[0.2624, -0.9531, -0.0054, 0.0026, 1.1633]),
	"freiburg2": dict(fx=520.9, fy=521.0, cx=325.1, cy=249.7, dist=[0.2312, -0.7849, -0.0033, -0.0001, 0.9172]),
	"freiburg3": dict(fx=535.4, fy=539.2, cx=320.1, cy=247.6, dist=[0.0, 0.0, 0.0, 0.0, 0.0]),
}
DEPTH_SCALE = 5000.0
TUM_FPS = 30.0


def read_table(path: Path) -> list:
	"""Read a whitespace separated TUM text file, skipping comments."""
	rows = []
	with open(path, "r") as f:
		for line in f:
			line = line.strip()
			if line and not line.startswith("#"):
				rows.append(line.split())
	return rows


def quat_to_matrix(q: np.ndarray) -> np.ndarray:
	"""Unit quaternion [qx, qy, qz, qw] to a 3x3 rotation matrix."""
	x, y, z, w = q / np.linalg.norm(q)
	return np.array([
		[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
		[2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
		[2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
	])


def slerp(q0: np.ndarray, q1: np.ndarray, a: float) -> np.ndarray:
	"""Spherical interpolation between unit quaternions."""
	d = float(np.dot(q0, q1))
	if d < 0.0:
		q1, d = -q1, -d
	if d > 0.9995:
		q = q0 + a * (q1 - q0)
		return q / np.linalg.norm(q)
	theta = np.arccos(d)
	return (np.sin((1 - a) * theta) * q0 + np.sin(a * theta) * q1) / np.sin(theta)


def interpolate_pose(gt_t: np.ndarray, gt: np.ndarray, t: float, max_gap: float):
	"""Camera pose at time t as a 4x4 matrix, or None if the trajectory has a gap there."""
	i = int(np.searchsorted(gt_t, t))
	if i <= 0 or i >= len(gt_t):
		return None
	t0, t1 = gt_t[i - 1], gt_t[i]
	if t1 - t0 > max_gap:
		return None
	a = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
	pos = (1 - a) * gt[i - 1, 1:4] + a * gt[i, 1:4]
	quat = slerp(gt[i - 1, 4:8], gt[i, 4:8], a)
	T = np.eye(4)
	T[:3, :3] = quat_to_matrix(quat)
	T[:3, 3] = pos
	return T


def main():
	parser = argparse.ArgumentParser(description="Convert a TUM RGB-D sequence for ImageSequenceDataset")
	parser.add_argument("sequence_dir", type=Path, help="Extracted TUM sequence (contains rgb.txt, depth.txt, groundtruth.txt)")
	parser.add_argument("output_dir", type=Path, help="Destination folder")
	parser.add_argument("--camera", choices=sorted(INTRINSICS), default=None,
		help="Kinect calibration to use (default: guessed from the folder name)")
	parser.add_argument("--stride", type=int, default=1, help="Keep every N-th RGB frame")
	parser.add_argument("--max-frames", type=int, default=None, help="Stop after this many written frames")
	parser.add_argument("--max-dt", type=float, default=0.02, help="Max RGB/depth timestamp difference in seconds")
	parser.add_argument("--max-gt-gap", type=float, default=0.1, help="Max gap in the ground-truth trajectory in seconds")
	parser.add_argument("--no-undistort", action="store_true", help="Keep the original (distorted) images")
	args = parser.parse_args()

	seq = args.sequence_dir
	camera = args.camera
	if camera is None:
		camera = next((c for c in INTRINSICS if c in seq.name), None)
		if camera is None:
			parser.error(f"cannot guess the camera from '{seq.name}', pass --camera")
	calib = INTRINSICS[camera]

	rgb_list = [(float(r[0]), r[1]) for r in read_table(seq / "rgb.txt")]
	depth_list = [(float(r[0]), r[1]) for r in read_table(seq / "depth.txt")]
	gt = np.array([[float(x) for x in r] for r in read_table(seq / "groundtruth.txt")])
	depth_t = np.array([t for t, _ in depth_list])
	gt_t = gt[:, 0]
	print(f"{seq.name}: {len(rgb_list)} rgb, {len(depth_list)} depth, {len(gt)} ground-truth poses, camera={camera}")

	first = cv2.imread(str(seq / rgb_list[0][1]), cv2.IMREAD_COLOR)
	h, w = first.shape[:2]
	K = np.array([[calib["fx"], 0.0, calib["cx"]], [0.0, calib["fy"], calib["cy"]], [0.0, 0.0, 1.0]])
	dist = np.array(calib["dist"])
	undistort = (not args.no_undistort) and bool(np.any(dist != 0.0))
	if undistort:
		map1, map2 = cv2.initUndistortRectifyMap(K, dist, None, K, (w, h), cv2.CV_32FC1)

	out = args.output_dir
	(out / "rgb").mkdir(parents=True, exist_ok=True)
	(out / "depth").mkdir(parents=True, exist_ok=True)
	(out / "pose").mkdir(parents=True, exist_ok=True)

	poses, stamps = [], []
	skipped = 0
	for k, (t_rgb, rgb_path) in enumerate(rgb_list):
		if k % args.stride != 0:
			continue
		j = int(np.argmin(np.abs(depth_t - t_rgb)))
		if abs(depth_t[j] - t_rgb) > args.max_dt:
			skipped += 1
			continue
		T = interpolate_pose(gt_t, gt, t_rgb, args.max_gt_gap)
		if T is None:
			skipped += 1
			continue

		rgb = cv2.imread(str(seq / rgb_path), cv2.IMREAD_COLOR)
		depth = cv2.imread(str(seq / depth_list[j][1]), cv2.IMREAD_UNCHANGED)
		if rgb is None or depth is None:
			skipped += 1
			continue
		if undistort:
			rgb = cv2.remap(rgb, map1, map2, cv2.INTER_LINEAR)
			depth = cv2.remap(depth, map1, map2, cv2.INTER_NEAREST)  # never blend depth values

		idx = len(poses)
		cv2.imwrite(str(out / "rgb" / f"{idx:06d}.png"), rgb)
		cv2.imwrite(str(out / "depth" / f"{idx:06d}.png"), depth)
		poses.append(T)
		stamps.append(t_rgb)
		if args.max_frames and len(poses) >= args.max_frames:
			break

	with open(out / "pose" / "poses.txt", "w") as f:
		for T in poses:
			f.write(" ".join(f"{v:.9g}" for v in T.reshape(-1)) + "\n")
	with open(out / "timestamps.txt", "w") as f:
		f.write("\n".join(f"{t:.6f}" for t in stamps) + "\n")
	info = {
		"width": int(w),
		"height": int(h),
		"intrinsics": K.tolist(),
		"distortion": [0.0] * 5 if undistort or not np.any(dist != 0.0) else dist.tolist(),
		"depth_scale": DEPTH_SCALE,
		"fps": TUM_FPS / args.stride,
		"source": f"TUM RGB-D {seq.name}",
	}
	with open(out / "camera_info.json", "w") as f:
		json.dump(info, f, indent=2)

	print(f"wrote {len(poses)} frames to {out} (skipped {skipped}); fps={info['fps']}, depth_scale={DEPTH_SCALE:g}")
	if poses:
		travelled = float(np.sum(np.linalg.norm(np.diff([T[:3, 3] for T in poses], axis=0), axis=1))) if len(poses) > 1 else 0.0
		print(f"trajectory length: {travelled:.1f} m over {stamps[-1] - stamps[0]:.1f} s")


if __name__ == "__main__":
	main()
