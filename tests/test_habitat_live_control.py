import importlib.util
import math
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("habitat_live_control_under_test", REPO / "scripts" / "habitat_live" / "control.py")
control = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = control
spec.loader.exec_module(control)


class Walker:
	"""A point that walks like the Habitat agent: forward is -z at yaw 0, a positive yaw turns left."""

	def __init__(self, x=0.0, z=0.0, yaw=0.0):
		self.x, self.z, self.yaw = x, z, yaw
		self.blocked = False

	def apply(self, actions):
		for name, count in actions:
			for _ in range(count):
				if name.startswith("turn"):
					self.yaw += control.STEP_DEG * (1 if name == "turn_left" else -1)
				elif name in ("move_forward", "move_backward") and not self.blocked:
					sign = 1 if name == "move_forward" else -1
					self.x += -math.sin(math.radians(self.yaw)) * control.STEP_M * sign
					self.z += -math.cos(math.radians(self.yaw)) * control.STEP_M * sign


class KeysTest(unittest.TestCase):
	def test_held_keys_make_a_motion(self) -> None:
		self.assertEqual(control.motion_from_keys({"w"}), control.Motion(forward=1.0))
		self.assertEqual(control.motion_from_keys({"s", "a"}), control.Motion(forward=-1.0, strafe=-1.0))
		self.assertEqual(control.motion_from_keys({"left", "up"}), control.Motion(turn=1.0, look=1.0))
		self.assertEqual(control.motion_from_keys({"q", "left"}).turn, 1.0, "two keys for the same direction do not add up")

	def test_opposite_keys_cancel_and_unknown_keys_do_nothing(self) -> None:
		self.assertTrue(control.motion_from_keys({"w", "s"}).is_idle())
		self.assertTrue(control.motion_from_keys({"z", "shift"}).is_idle())
		self.assertTrue(control.motion_from_keys(set()).is_idle())


class ActionBudgetTest(unittest.TestCase):
	def walked(self, fps, seconds=1.0, boost=False):
		budget = control.ActionBudget(speed=0.6, turn_rate=70.0)
		total = {"move_forward": 0, "turn_left": 0}
		for _ in range(int(fps * seconds)):
			for name, count in budget.actions(control.Motion(forward=1.0, turn=1.0), 1.0 / fps, boost=boost):
				total[name] += count
		return total["move_forward"] * control.STEP_M, total["turn_left"] * control.STEP_DEG

	def test_speed_does_not_depend_on_the_frame_rate(self) -> None:
		for fps in (20, 30, 60, 144):
			metres, degrees = self.walked(fps)
			self.assertAlmostEqual(metres, 0.6, delta=0.011, msg=f"{fps} frames/s")
			self.assertAlmostEqual(degrees, 70.0, delta=0.6, msg=f"{fps} frames/s")

	def test_shift_walks_faster_but_does_not_turn_faster(self) -> None:
		metres, degrees = self.walked(60, boost=True)
		self.assertAlmostEqual(metres, 1.2, delta=0.011)
		self.assertAlmostEqual(degrees, 70.0, delta=0.6)

	def test_a_remainder_is_dropped_when_the_key_is_released(self) -> None:
		budget = control.ActionBudget(speed=0.6)
		self.assertEqual(budget.actions(control.Motion(forward=1.0), 0.005), [])  # 3 mm: less than one action
		self.assertEqual(budget.actions(control.Motion(), 0.1), [])
		self.assertEqual(budget.actions(control.Motion(forward=1.0), 0.005), [], "the old 3 mm must not come back")

	def test_backwards_and_strafing_have_their_own_actions(self) -> None:
		budget = control.ActionBudget(speed=0.6)
		names = dict(budget.actions(control.Motion(forward=-1.0, strafe=1.0, turn=-1.0, look=1.0), 1 / 30))
		self.assertEqual(set(names), {"move_backward", "move_right", "turn_right", "look_up"})

	def test_a_long_pause_cannot_cause_a_burst_of_actions(self) -> None:
		budget = control.ActionBudget(speed=0.6, max_actions=40)
		(name, count), = budget.actions(control.Motion(forward=1.0), 30.0)
		self.assertEqual((name, count), ("move_forward", 40))


class GeometryTest(unittest.TestCase):
	def test_headings(self) -> None:
		self.assertAlmostEqual(control.heading_to(0, 0, 0, -5), 0.0)  # -z is ahead
		self.assertAlmostEqual(control.heading_to(0, 0, -5, 0), 90.0)  # -x is on the left
		self.assertAlmostEqual(control.heading_to(0, 0, 5, 0), -90.0)
		self.assertAlmostEqual(abs(control.heading_to(0, 0, 0, 5)), 180.0)

	def test_yaw_of_a_rotation_about_y(self) -> None:
		for degrees in (-120, -30, 0, 45, 170):
			c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
			matrix = [[c, 0, s], [0, 1, 0], [-s, 0, c]]
			self.assertAlmostEqual(control.yaw_from_rotation_matrix(matrix), degrees, places=6)
			forward = (-matrix[0][2], -matrix[2][2])  # the body's -z axis
			self.assertAlmostEqual(control.heading_to(0, 0, forward[0], forward[1]), degrees, places=6, msg="heading_to and yaw agree")

	def test_wrapping(self) -> None:
		self.assertAlmostEqual(control.wrap_degrees(190), -170)
		self.assertAlmostEqual(control.wrap_degrees(-190), 170)
		self.assertAlmostEqual(control.wrap_degrees(180), 180)
		self.assertAlmostEqual(control.wrap_degrees(720), 0)

	def test_the_next_goal_is_the_one_farthest_from_everything_seen(self) -> None:
		candidates = [(1.0, 0.0), (6.0, 0.0), (3.0, 1.0)]
		self.assertEqual(control.choose_goal(candidates, [(0.0, 0.0), (3.0, 0.0)]), (6.0, 0.0))
		self.assertEqual(control.choose_goal(candidates, []), (1.0, 0.0))
		self.assertIsNone(control.choose_goal([], [(0.0, 0.0)]))


class AutopilotTest(unittest.TestCase):
	def drive(self, pilot, walker, seconds, fps=30):
		budget = control.ActionBudget(speed=0.6, turn_rate=70.0)
		for _ in range(int(seconds * fps)):
			motion = pilot.motion(walker.x, walker.z, walker.yaw, 1.0 / fps)
			walker.apply(budget.actions(motion, 1.0 / fps))

	def test_it_walks_a_route_through_its_waypoints(self) -> None:
		corners = [(0.0, -2.0), (2.0, -2.0), (2.0, 1.0), (-1.0, 1.0)]
		routes = [list(corners)]
		pilot = control.Autopilot(lambda x, z, visited: routes.pop() if routes else None)
		walker = Walker()
		reached = []
		budget = control.ActionBudget(speed=0.6, turn_rate=70.0)
		for _ in range(60 * 30):
			motion = pilot.motion(walker.x, walker.z, walker.yaw, 1 / 30)
			walker.apply(budget.actions(motion, 1 / 30))
			for corner in corners:
				if corner not in reached and math.hypot(corner[0] - walker.x, corner[1] - walker.z) < control.Autopilot(None).reach:
					reached.append(corner)
		self.assertEqual(reached, corners, "all waypoints were reached, in order")

	def test_it_asks_for_a_new_route_when_the_old_one_is_done_and_remembers_where_it_was(self) -> None:
		calls = []

		def planner(x, z, visited):
			calls.append(len(visited))
			return [(x + 1.5, z)]

		pilot = control.Autopilot(planner)
		self.drive(pilot, Walker(yaw=-90.0), seconds=20)  # yaw -90 faces +x
		self.assertGreaterEqual(len(calls), 4)
		self.assertEqual(calls, sorted(calls), "the visited list only grows")
		self.assertGreater(pilot.visited[-1][0], 4.0)

	def test_it_stands_still_when_nothing_is_reachable(self) -> None:
		pilot = control.Autopilot(lambda x, z, visited: None)
		self.assertTrue(pilot.motion(0.0, 0.0, 0.0, 0.1).is_idle())

	def test_it_gives_up_a_waypoint_that_it_cannot_reach(self) -> None:
		routes = [[(0.0, -3.0), (0.0, -6.0)]]
		pilot = control.Autopilot(lambda x, z, visited: routes.pop() if routes else None, stuck_seconds=2.0)
		walker = Walker()
		walker.blocked = True  # pressed against a wall
		self.drive(pilot, walker, seconds=6)
		self.assertGreaterEqual(pilot.skipped, 1)


if __name__ == "__main__":
	unittest.main()
