"""Per (frame, track) observation records (sections A, B): camera motion, object and frame motion blur, image sharpness,
q' = q * w_blur, viewing geometry, area and truncation. Plain numpy so the formulas are testable without the pipeline."""
import json
import math
from pathlib import Path

import cv2
import numpy as np


def rotation(quaternion):
	x, y, z, w = quaternion
	return np.array([
		[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
		[2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
		[2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def pose(transform):
	"""T_world_camera from DAAAM's [x, y, z, qx, qy, qz, qw], or None."""
	if transform is None:
		return None
	values = np.asarray(transform, dtype=np.float64).ravel()
	if values.size != 7 or not np.all(np.isfinite(values)):
		return None
	T = np.eye(4)
	T[:3, :3] = rotation(values[3:] / np.linalg.norm(values[3:]))
	T[:3, 3] = values[:3]
	return T


def rotation_vector(R):
	return cv2.Rodrigues(np.asarray(R, dtype=np.float64))[0].ravel()


class Motion:
	"""Camera-frame angular velocity omega, linear velocity v and exposure of a frame (A). From the motion file exported from
	the recording (gyroscope, odometry, RealSense exposure at mid-exposure) when given, else differences of consecutive poses
	with the configured exposure."""

	def __init__(self, path, fps, exposure_s):
		self.rows, self.fps, self.exposure = {}, fps, exposure_s
		if path:
			data = json.loads(Path(path).read_text())
			self.rows = {int(row['index']): row for row in data['frames']}
		self.previous = None

	def at(self, frame):
		T = pose(frame.transform)
		previous, self.previous = self.previous, (T, frame.timestamp)
		if self.rows:
			row = self.rows.get(int(round(frame.timestamp * self.fps)))
			if row is None or row.get('omega_cam') is None:
				return None, 'motion_file_missing_row' if row is None else (row.get('reason') or 'motion_file_no_gyro')
			return dict(omega=np.asarray(row['omega_cam'], float), v=np.asarray(row.get('v_cam') or [0, 0, 0], float),
				exposure=float(row.get('exposure_s') or self.exposure), source='motion_file'), None
		if T is None or previous is None or previous[0] is None:
			return None, 'pose_unavailable'
		dt = frame.timestamp - previous[1]
		if not dt > 0:
			return None, 'pose_unavailable'
		R0, R1 = previous[0][:3, :3], T[:3, :3]
		omega = rotation_vector(R0.T @ R1) / dt  # body (camera) frame
		v = R1.T @ (T[:3, 3] - previous[0][:3, 3]) / dt
		return dict(omega=omega, v=v, exposure=self.exposure, source='pose_difference'), None


def project(points, K):
	return np.stack([K[0, 0] * points[:, 0] / points[:, 2] + K[0, 2], K[1, 1] * points[:, 1] / points[:, 2] + K[1, 2]], axis=1)


def pixel_motion(points, omega, v, exposure, K):
	"""Image displacement of camera-frame points during the exposure: X' = X + (-omega x X - v) t_exp. Returns (b, direction)."""
	points = np.asarray(points, dtype=np.float64)
	moved = points + (-np.cross(omega, points) - v) * exposure
	keep = (points[:, 2] > 0.05) & (moved[:, 2] > 0.05)
	if not keep.any():
		return None, None
	shift = project(moved[keep], K) - project(points[keep], K)
	lengths = np.linalg.norm(shift, axis=1)
	worst = int(np.argmax(lengths))
	b = float(lengths[worst])
	return b, (shift[worst] / b if b > 1e-9 else None)


def frame_rotation_blur(omega, exposure, K, width, height):
	"""b_rot: rotation-only blur at the image corners and centre (independent of depth)."""
	pixels = np.array([[0, 0], [width - 1, 0], [0, height - 1], [width - 1, height - 1], [width / 2, height / 2]], float)
	rays = np.column_stack([(pixels[:, 0] - K[0, 2]) / K[0, 0], (pixels[:, 1] - K[1, 2]) / K[1, 1], np.ones(len(pixels))])
	return pixel_motion(rays, omega, np.zeros(3), exposure, K)[0]


def sharpness_ratio(gray, mask, direction):
	"""rho: mean gradient magnitude along the blur direction over that perpendicular to it, inside the mask. Without a known
	direction the weakest gradient direction of the structure tensor is used. rho well below 1 means blurred along it."""
	gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
	gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
	region = cv2.dilate(mask.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
	gx, gy = gx[region].astype(np.float64), gy[region].astype(np.float64)
	if gx.size < 16:
		return None
	if direction is None:
		tensor = np.array([[np.mean(gx * gx), np.mean(gx * gy)], [np.mean(gx * gy), np.mean(gy * gy)]])
		direction = np.linalg.eigh(tensor)[1][:, 0]
	dx, dy = direction
	along, across = np.mean(np.abs(gx * dx + gy * dy)), np.mean(np.abs(-gx * dy + gy * dx))
	return float(along / across) if across > 1e-9 else None


def blur_weight(b, rho, cfg):
	"""w_blur from the configured source; 0 when the predicted blur exceeds blur_max_px."""
	w_odo = None if b is None else math.exp(-b * b / (2 * cfg.blur_b0_px ** 2))
	w_img = None if rho is None else min(max(rho, 0.0), 1.0)
	if b is not None and b > cfg.blur_max_px and cfg.blur_source != 'image':
		return 0.0
	w_odo, w_img = 1.0 if w_odo is None else w_odo, 1.0 if w_img is None else w_img  # a missing source does not penalize
	if cfg.blur_source == 'odometry':
		return w_odo
	if cfg.blur_source == 'image':
		return w_img
	return w_odo ** cfg.blur_joint_a * w_img ** (1 - cfg.blur_joint_a)


def truncation(mask):
	"""Mask pixels on the image border over the bbox perimeter (0 when the mask does not touch the border)."""
	ys, xs = np.nonzero(mask)
	if xs.size == 0:
		return 0.0
	edge = mask[0, :].sum() + mask[-1, :].sum() + mask[:, 0].sum() + mask[:, -1].sum()
	return float(edge) / float(2 * (xs.max() - xs.min() + 1) + 2 * (ys.max() - ys.min() + 1))


def sample_pixels(mask, count):
	ys, xs = np.nonzero(mask)
	if xs.size == 0:
		return xs, ys
	index = np.linspace(0, xs.size - 1, min(count, xs.size)).astype(int)
	return xs[index], ys[index]


def intrinsics(frame):
	k = frame.camera_intrinsics or {}
	if not all(key in k for key in ('fx', 'fy', 'cx', 'cy')):
		return None
	return np.array([[k['fx'], 0, k['cx']], [0, k['fy'], k['cy']], [0, 0, 1.0]])


def observe(frame, track, mask, q, motion, cfg, center_hint=None):
	"""Observation record of one track in one frame. motion is Motion.at(frame)[0] (None without camera motion); center_hint
	is the object's world centre for blur when its mask has no valid depth."""
	h, w = mask.shape
	K, T = intrinsics(frame), pose(frame.transform)
	row = dict(frame_id=int(frame.frame_id), track_id=int(track.id), timestamp=float(frame.timestamp), q=float(q),
		area_ratio=float(mask.sum()) / float(h * w), edge_ratio=truncation(mask), bbox=[int(v) for v in cv2.boundingRect(mask.astype(np.uint8))])
	row['truncated'] = row['edge_ratio'] > cfg.truncation_max
	points = None
	if K is not None and frame.depth_image is not None:
		xs, ys = sample_pixels(mask, max(cfg.blur_samples, 64))
		z = frame.depth_image[ys, xs].astype(np.float64)
		ok = np.isfinite(z) & (z > 0)
		if ok.any():
			points = np.column_stack([(xs[ok] - K[0, 2]) * z[ok] / K[0, 0], (ys[ok] - K[1, 2]) * z[ok] / K[1, 1], z[ok]])
	if T is not None:
		row['camera_world'] = T[:3, 3].tolist()
		if points is not None:
			row['center_world'] = (T[:3, :3] @ np.median(points, axis=0) + T[:3, 3]).tolist()
			row['distance'] = float(np.linalg.norm(np.median(points, axis=0)))
	b = b_dir = b_rot = rho = None
	if cfg.blur:
		if motion is not None and K is not None:
			blur_points = points
			if blur_points is None and center_hint is not None and T is not None:
				blur_points = (T[:3, :3].T @ (np.asarray(center_hint) - T[:3, 3]))[None, :]
			if blur_points is not None:
				blur_points = blur_points[np.linspace(0, len(blur_points) - 1, min(cfg.blur_samples, len(blur_points))).astype(int)]
				b, b_dir = pixel_motion(blur_points, motion['omega'], motion['v'], motion['exposure'], K)
			b_rot = frame_rotation_blur(motion['omega'], motion['exposure'], K, w, h)
			row['exposure_s'] = motion['exposure']
			row['motion_source'] = motion['source']
		if cfg.blur_source != 'odometry':
			x, y, bw, bh = row['bbox']
			gray = cv2.cvtColor(frame.rgb_image[y:y + bh, x:x + bw], cv2.COLOR_RGB2GRAY)
			rho = sharpness_ratio(gray, mask[y:y + bh, x:x + bw], b_dir)
	w_blur = blur_weight(b, rho, cfg) if cfg.blur else 1.0
	row.update(b=b, b_direction=None if b_dir is None else [float(b_dir[0]), float(b_dir[1])], b_rot=b_rot, rho=rho,
		w_blur=float(w_blur), q_prime=float(q * w_blur))
	return row


def crop(mask, margin):
	"""A copy of the object's mask inside its bbox grown by margin on each side, plus that window's offset. The RGB frame is
	referenced, not copied: frames are read-only and one frame serves every object that keeps it."""
	x, y, w, h = cv2.boundingRect(mask.astype(np.uint8))
	dx, dy = int(round(w * margin)), int(round(h * margin))
	x0, y0 = max(x - dx, 0), max(y - dy, 0)
	x1, y1 = min(x + w + dx, mask.shape[1]), min(y + h + dy, mask.shape[0])
	return np.array(mask[y0:y1, x0:x1] > 0, copy=True), (x0, y0)
