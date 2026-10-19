"""Keyboard and autopilot control of the Habitat bridge: pure Python, no habitat-sim and no window toolkit.

The bridge turns the keys that are held down into a `Motion` (what fraction of full speed along four axes), and an
`ActionBudget` turns that into whole simulator actions of a fixed small size per frame, so that the speed does not depend
on how fast the window loop runs. The `Autopilot` produces the same `Motion` from a route instead of from the keys.
"""

import math
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

STEP_M = 0.01  # metres of one move action
STEP_DEG = 0.5  # degrees of one turn or look action

# logical key -> (axis, direction); `forward` +1 = ahead, `strafe` +1 = right, `turn` +1 = left, `look` +1 = up
BINDINGS: Dict[str, Tuple[str, int]] = {
	"w": ("forward", 1), "s": ("forward", -1), "d": ("strafe", 1), "a": ("strafe", -1),
	"left": ("turn", 1), "right": ("turn", -1), "q": ("turn", 1), "e": ("turn", -1),
	"up": ("look", 1), "down": ("look", -1),
}
ACTIONS = {
	("forward", 1): "move_forward", ("forward", -1): "move_backward",
	("strafe", 1): "move_right", ("strafe", -1): "move_left",
	("turn", 1): "turn_left", ("turn", -1): "turn_right",
	("look", 1): "look_up", ("look", -1): "look_down",
}


@dataclass(frozen=True)
class Motion:
	forward: float = 0.0
	strafe: float = 0.0
	turn: float = 0.0
	look: float = 0.0

	def is_idle(self) -> bool:
		return not (self.forward or self.strafe or self.turn or self.look)


def motion_from_keys(pressed: Iterable[str]) -> Motion:
	"""The motion that a set of held logical keys asks for (opposite keys cancel)."""
	axes = {"forward": 0, "strafe": 0, "turn": 0, "look": 0}
	for key in set(pressed):
		if key in BINDINGS:
			axis, sign = BINDINGS[key]
			axes[axis] += sign
	return Motion(**{axis: float(max(-1, min(1, value))) for axis, value in axes.items()})


class ActionBudget:
	"""Whole simulator actions per frame for continuous speeds: 0.6 m/s is one 1 cm action per frame at 60 frames/s."""

	def __init__(self, speed: float = 0.6, turn_rate: float = 70.0, look_rate: float = 40.0, boost: float = 2.0, max_actions: int = 40):
		self.rates = {"forward": speed, "strafe": speed, "turn": turn_rate, "look": look_rate}
		self.steps = {"forward": STEP_M, "strafe": STEP_M, "turn": STEP_DEG, "look": STEP_DEG}
		self.boost = boost
		self.max_actions = max_actions
		self._carry = {axis: 0.0 for axis in self.rates}

	def actions(self, motion: Motion, dt: float, boost: bool = False) -> List[Tuple[str, int]]:
		"""[(action name, repetitions)] to run for `dt` seconds of `motion`; the remainder carries over to the next frame."""
		result = []
		for axis, rate in self.rates.items():
			value = getattr(motion, axis)
			if not value:
				self._carry[axis] = 0.0
				continue
			self._carry[axis] += value * rate * (self.boost if boost and axis in ("forward", "strafe") else 1.0) * dt
			step = self.steps[axis]
			count = int(abs(self._carry[axis]) / step)
			if count:
				sign = 1 if self._carry[axis] > 0 else -1
				self._carry[axis] -= sign * count * step
				result.append((ACTIONS[(axis, sign)], min(count, self.max_actions)))
		return result


def wrap_degrees(angle: float) -> float:
	"""Angle in degrees in (-180, 180]."""
	angle = math.fmod(angle + 180.0, 360.0)
	if angle <= 0:
		angle += 360.0
	return angle - 180.0


def heading_to(x: float, z: float, target_x: float, target_z: float) -> float:
	"""Yaw in degrees that faces a point: Habitat looks along -z at yaw 0, and a positive yaw turns left."""
	return math.degrees(math.atan2(-(target_x - x), -(target_z - z)))


def yaw_from_rotation_matrix(matrix: Sequence[Sequence[float]]) -> float:
	"""Yaw in degrees of a body whose forward axis is -z (rotation about +y, as Habitat's agent turns)."""
	return math.degrees(math.atan2(matrix[0][2], matrix[2][2]))


def choose_goal(candidates: Sequence[Tuple[float, float]], visited: Sequence[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
	"""The candidate that is farthest from everything visited so far (the first one when nothing was visited)."""
	if not candidates:
		return None
	if not visited:
		return candidates[0]
	return max(candidates, key=lambda c: min(math.hypot(c[0] - v[0], c[1] - v[1]) for v in visited))


class Autopilot:
	"""Walks a route of (x, z) waypoints like a person on the keyboard: turns on the spot when the target is off to the
	side, walks when it is ahead, slows down near a waypoint. `planner(x, z, visited)` returns the next route.
	"""

	def __init__(
		self,
		planner: Callable[[float, float, Sequence[Tuple[float, float]]], Optional[List[Tuple[float, float]]]],
		reach: float = 0.3,
		stuck_seconds: float = 4.0,
		mark_every: float = 0.5,
	):
		self.planner = planner
		self.reach = reach
		self.stuck_seconds = stuck_seconds
		self.mark_every = mark_every
		self.route: List[Tuple[float, float]] = []
		self.visited: List[Tuple[float, float]] = []
		self.replans = 0
		self.skipped = 0
		self._watch = None  # (time, x, z) where progress was last seen
		self._clock = 0.0

	def motion(self, x: float, z: float, yaw: float, dt: float) -> Motion:
		self._clock += dt
		if not self.visited or math.hypot(x - self.visited[-1][0], z - self.visited[-1][1]) >= self.mark_every:
			self.visited.append((x, z))
		while self.route and math.hypot(self.route[0][0] - x, self.route[0][1] - z) < self.reach:
			self.route.pop(0)
			self._watch = None
		if not self.route:
			self.route = list(self.planner(x, z, self.visited) or [])
			self.replans += 1
			self._watch = None
			if not self.route:
				return Motion()
		if self._watch is None or math.hypot(x - self._watch[1], z - self._watch[2]) >= 0.15:
			self._watch = (self._clock, x, z)
		elif self._clock - self._watch[0] > self.stuck_seconds:  # pressed against something: give this waypoint up
			self.route.pop(0)
			self.skipped += 1
			self._watch = None
			return Motion(turn=1.0)
		target_x, target_z = self.route[0]
		error = wrap_degrees(heading_to(x, z, target_x, target_z) - yaw)
		distance = math.hypot(target_x - x, target_z - z)
		turn = max(-1.0, min(1.0, error / 30.0))
		ahead = max(0.0, 1.0 - abs(error) / 40.0)
		forward = ahead * (0.5 if distance < 0.6 else 1.0)
		return Motion(forward=forward, turn=turn)
