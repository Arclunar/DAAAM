"""Rigid transforms, quaternions and trajectory interpolation (numpy only).

Conventions: quaternions are [qx, qy, qz, qw] (ROS/TUM order); a pose T_a_b maps points of frame b into frame a
(p_a = T_a_b @ p_b), so a camera pose T_world_camera is the camera-to-world transform; cameras are optical frames
(x right, y down, z forward).
"""

from typing import Optional, Sequence, Tuple

import numpy as np


def quat_to_matrix(q: Sequence[float]) -> np.ndarray:
	"""Unit quaternion [qx, qy, qz, qw] to a 3x3 rotation matrix."""
	x, y, z, w = np.asarray(q, dtype=np.float64) / np.linalg.norm(q)
	return np.array([
		[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
		[2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
		[2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
	])


def matrix_to_quat(R: np.ndarray) -> np.ndarray:
	"""3x3 rotation matrix to a unit quaternion [qx, qy, qz, qw] with qw >= 0."""
	R = np.asarray(R, dtype=np.float64)
	trace = R[0, 0] + R[1, 1] + R[2, 2]
	if trace > 0:
		s = 2.0 * np.sqrt(trace + 1.0)
		q = [(R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s, 0.25 * s]
	elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
		s = 2.0 * np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2])
		q = [0.25 * s, (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s, (R[2, 1] - R[1, 2]) / s]
	elif R[1, 1] > R[2, 2]:
		s = 2.0 * np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2])
		q = [(R[0, 1] + R[1, 0]) / s, 0.25 * s, (R[1, 2] + R[2, 1]) / s, (R[0, 2] - R[2, 0]) / s]
	else:
		s = 2.0 * np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1])
		q = [(R[0, 2] + R[2, 0]) / s, (R[1, 2] + R[2, 1]) / s, 0.25 * s, (R[1, 0] - R[0, 1]) / s]
	q = np.asarray(q)
	q /= np.linalg.norm(q)
	return -q if q[3] < 0 else q


def pose_matrix(translation: Sequence[float], quat: Sequence[float]) -> np.ndarray:
	"""4x4 transform from a translation and a quaternion [qx, qy, qz, qw]."""
	T = np.eye(4)
	T[:3, :3] = quat_to_matrix(quat)
	T[:3, 3] = np.asarray(translation, dtype=np.float64)
	return T


def invert(T: np.ndarray) -> np.ndarray:
	"""Inverse of a rigid 4x4 transform (or a stack of them)."""
	T = np.asarray(T, dtype=np.float64)
	R = T[..., :3, :3]
	t = T[..., :3, 3:]
	out = np.zeros_like(T)
	out[..., :3, :3] = np.swapaxes(R, -1, -2)
	out[..., :3, 3:] = -np.swapaxes(R, -1, -2) @ t
	out[..., 3, 3] = 1.0
	return out


def is_rigid(T: np.ndarray, tol: float = 1e-4) -> bool:
	"""True for a finite 4x4 transform with an orthonormal, right-handed rotation."""
	T = np.asarray(T, dtype=np.float64)
	if T.shape != (4, 4) or not np.all(np.isfinite(T)):
		return False
	R = T[:3, :3]
	return (np.allclose(R @ R.T, np.eye(3), atol=tol) and abs(np.linalg.det(R) - 1.0) < tol
		and np.allclose(T[3], [0, 0, 0, 1], atol=tol))


def slerp(q0: np.ndarray, q1: np.ndarray, a: float) -> np.ndarray:
	"""Spherical interpolation between unit quaternions [qx, qy, qz, qw]."""
	q0 = np.asarray(q0, dtype=np.float64)
	q1 = np.asarray(q1, dtype=np.float64)
	d = float(np.dot(q0, q1))
	if d < 0.0:
		q1, d = -q1, -d
	if d > 0.9995:
		q = q0 + a * (q1 - q0)
		return q / np.linalg.norm(q)
	theta = np.arccos(d)
	return (np.sin((1 - a) * theta) * q0 + np.sin(a * theta) * q1) / np.sin(theta)


def interpolate_poses(sample_t: np.ndarray, sample_T: np.ndarray, query_t: np.ndarray,
		max_gap: float) -> Tuple[np.ndarray, np.ndarray]:
	"""Poses at query times by lerp + slerp between the bracketing samples.

	Returns (poses [K, 4, 4], ok [K] bool); a query outside the samples or inside a gap longer than max_gap seconds is
	not ok (its pose is the identity).
	"""
	sample_t = np.asarray(sample_t, dtype=np.float64)
	query_t = np.asarray(query_t, dtype=np.float64)
	order = np.argsort(sample_t, kind="stable")
	sample_t = sample_t[order]
	sample_T = np.asarray(sample_T, dtype=np.float64)[order]
	quats = np.array([matrix_to_quat(T[:3, :3]) for T in sample_T])
	out = np.tile(np.eye(4), (len(query_t), 1, 1))
	ok = np.zeros(len(query_t), dtype=bool)
	for k, t in enumerate(query_t):
		i = int(np.searchsorted(sample_t, t, side="right"))  # sample_t[i - 1] <= t < sample_t[i]
		if i > 0 and sample_t[i - 1] == t:
			out[k] = sample_T[i - 1]  # exactly a sample
			ok[k] = True
			continue
		if i <= 0 or i >= len(sample_t):
			continue
		t0, t1 = sample_t[i - 1], sample_t[i]
		if t1 - t0 > max_gap:
			continue
		a = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
		out[k, :3, :3] = quat_to_matrix(slerp(quats[i - 1], quats[i], a))
		out[k, :3, 3] = (1 - a) * sample_T[i - 1, :3, 3] + a * sample_T[i, :3, 3]
		ok[k] = True
	return out, ok


def rotation_from_euler_deg(rx: float, ry: float, rz: float) -> np.ndarray:
	"""Rotation R = Rz @ Ry @ Rx from angles in degrees about the x, y and z axes."""
	ax, ay, az = np.radians([rx, ry, rz])
	cx, sx, cy, sy, cz, sz = np.cos(ax), np.sin(ax), np.cos(ay), np.sin(ay), np.cos(az), np.sin(az)
	Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
	Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
	Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
	return Rz @ Ry @ Rx


def offset_in_camera_frame(poses: np.ndarray, translation: Sequence[float] = (0.0, 0.0, 0.0),
		rotation_deg: Sequence[float] = (0.0, 0.0, 0.0)) -> np.ndarray:
	"""Move every camera by a fixed offset expressed in its own optical frame (x right, y down, z forward).

	T_world_camera' = T_world_camera @ [R | t]: the camera is first shifted by t along its own axes, then turned by R.
	"""
	delta = np.eye(4)
	delta[:3, :3] = rotation_from_euler_deg(*rotation_deg)
	delta[:3, 3] = np.asarray(translation, dtype=np.float64)
	return np.asarray(poses, dtype=np.float64) @ delta


def rotation_angle_deg(R: np.ndarray) -> float:
	"""Angle of a rotation matrix in degrees."""
	c = (np.trace(np.asarray(R, dtype=np.float64)) - 1.0) / 2.0
	return float(np.degrees(np.arccos(np.clip(c, -1.0, 1.0))))


def path_length(poses: np.ndarray) -> float:
	"""Total distance travelled by the camera centres in metres."""
	centres = np.asarray(poses)[:, :3, 3]
	return float(np.sum(np.linalg.norm(np.diff(centres, axis=0), axis=1))) if len(centres) > 1 else 0.0


def scene_extent(poses: np.ndarray, minimum: Optional[float] = 0.5) -> float:
	"""1.1 x the largest distance of a camera centre from their mean (the 3DGS 'scene scale')."""
	centres = np.asarray(poses)[:, :3, 3]
	extent = 1.1 * float(np.max(np.linalg.norm(centres - centres.mean(axis=0), axis=1))) if len(centres) else 0.0
	return max(extent, minimum or 0.0)
