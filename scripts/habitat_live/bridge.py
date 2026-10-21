#!/usr/bin/env python3
"""Habitat bridge: steer a habitat-sim agent with the keyboard while DAAAM maps what it sees.

Runs in an environment that has habitat-sim (the `habitat-smallroom` conda environment of ~/warmoon_ws/habitat_ws on
this machine; nothing is installed into it), not in the DAAAM container. It opens a window with the agent's first-person
view, moves the agent with the keys that are held down, and serves the newest RGB-D frame, pose and intrinsics over a
local TCP socket (src/daaam/datasets/live_protocol.py) to `scripts/run_pipeline.py --dataset-type LiveDataset`.

    GALLIUM_DRIVER=d3d12 MESA_D3D12_DEFAULT_ADAPTER_NAME=NVIDIA \\
        ~/miniforge3/envs/habitat-smallroom/bin/python scripts/habitat_live/bridge.py [--autopilot] [--port 8002]

`scripts/habitat_live.sh` (`just habitat-live`) starts it together with the mapper. Keys: W/S walk, A/D strafe,
Left/Right (or Q/E) turn, Up/Down look, Shift faster, R back to the start, P autopilot, H hide the text, X stop, Esc quit.
The container shares the host's network, so the mapper reaches 127.0.0.1:8002 directly.
"""

import argparse
import importlib.util
import json
import math
import os
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DEFAULT_DATA = Path.home() / "warmoon_ws" / "habitat_ws" / "data"
START = {"apt_1": (2.1, 0.11937291920185089, 2.2)}  # the living room of ReplicaCAD apt_1, looking at the kitchen
FONT = "ProggyClean.ttf"
# LiveDataset's runner reads frame 0 once to set up the camera (1st capture), again as the first frame of the loop after the
# models are loaded (2nd), then maps (3rd on): the autopilot and --run-seconds begin there
MAPPING_FROM_FRAME = 3


def parse_args(argv=None):
	parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
	parser.add_argument("--scene", default="apt_1", help="scene of the dataset (default: the ReplicaCAD apartment apt_1), or a scene file")
	parser.add_argument("--scene-dataset", default=None, help="scene dataset config (default: replica_cad/replicaCAD.scene_dataset_config.json under $HABITAT_DATA)")
	parser.add_argument("--start", type=float, nargs=3, metavar=("X", "Y", "Z"), help="start position (default: the living room of apt_1, else a random navigable point)")
	parser.add_argument("--start-yaw", type=float, default=0.0, help="start heading in degrees (0 looks along -z, positive turns left)")
	parser.add_argument("--width", type=int, default=640)
	parser.add_argument("--height", type=int, default=480)
	parser.add_argument("--hfov", type=float, default=90.0, help="horizontal field of view in degrees")
	parser.add_argument("--camera-height", type=float, default=1.2, help="metres above the floor")
	parser.add_argument("--near", type=float, default=0.05)
	parser.add_argument("--far", type=float, default=10.0, help="depth beyond this is reported as no measurement")
	parser.add_argument("--agent-radius", type=float, default=0.18)
	parser.add_argument("--agent-height", type=float, default=1.5)
	parser.add_argument("--speed", type=float, default=0.6, help="walking speed in m/s (Shift: twice)")
	parser.add_argument("--turn-rate", type=float, default=70.0, help="turning speed in degrees/s")
	parser.add_argument("--look-rate", type=float, default=40.0, help="pitch speed in degrees/s")
	parser.add_argument("--host", default="127.0.0.1")
	parser.add_argument("--port", type=int, default=8002)
	parser.add_argument("--fps-limit", type=float, default=60.0, help="frame rate of the window loop")
	parser.add_argument("--autopilot", action="store_true", help="start in autopilot mode: once the mapper maps, walk to places that were not seen yet (P toggles it)")
	parser.add_argument("--run-seconds", type=float, default=0.0, help="close the stream this many seconds after the mapper started to map (its third frame; 0 = until Esc)")
	parser.add_argument("--seed", type=int, default=0)
	parser.add_argument("--stats-file", default=None, help="write the render statistics of the session here (JSON) at exit")
	parser.add_argument("--no-hud", action="store_true", help="no text over the view")
	parser.add_argument("--check", action="store_true", help="create the simulator, render one frame, print its numbers and exit")
	return parser.parse_args(argv)


def load_protocol():
	"""src/daaam/datasets/live_protocol.py by path: this environment has no `daaam` package."""
	path = REPO / "src" / "daaam" / "datasets" / "live_protocol.py"
	spec = importlib.util.spec_from_file_location("daaam_live_protocol", path)
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


def load_control():
	spec = importlib.util.spec_from_file_location("habitat_live_control", HERE / "control.py")
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


def percentile(values, q):
	values = sorted(values)
	return values[min(len(values) - 1, int(len(values) * q))] if values else 0.0


def run(args):
	# The GL variables must be set before the context exists; a GLFW window owns the context (EGL fails to match CUDA on
	# WSL2), and habitat_sim may only be imported after it exists.
	os.environ.setdefault("GALLIUM_DRIVER", "d3d12")
	os.environ.setdefault("MESA_D3D12_DEFAULT_ADAPTER_NAME", "NVIDIA")
	import ctypes
	sys.setdlopenflags(sys.getdlopenflags() | ctypes.RTLD_GLOBAL)
	import magnum as mn
	import numpy as np
	import quaternion
	from magnum.platform.glfw import Application

	protocol = load_protocol()
	control = load_control()

	class Bridge(Application):
		def __init__(self):
			configuration = self.Configuration()
			configuration.title = "DAAAM live bridge (Habitat)"
			configuration.size = (args.width, args.height)
			super().__init__(configuration)
			gl = ctypes.CDLL("libGL.so.1")
			gl.glGetString.restype = ctypes.c_char_p
			self.renderer_name = gl.glGetString(0x1F01).decode()
			if "llvmpipe" in self.renderer_name.lower():
				print("[bridge] WARNING: software OpenGL (llvmpipe); the GL variables of this script select the GPU (GALLIUM_DRIVER=d3d12)", flush=True)
			self._setup_sim()
			self._setup_hud()
			self.keys = set()
			self.boost = False
			self.budget = control.ActionBudget(args.speed, args.turn_rate, args.look_rate)
			self.autopilot = control.Autopilot(self._plan_route)
			self.autopilot_on = args.autopilot
			self.hud_on = not args.no_hud
			self.quit = False
			self.server = protocol.FrameServer(args.host, args.port)
			self.server.start()
			self.first_served = 0.0
			self.last_tick = self.started = time.perf_counter()
			self.loop_times, self.capture_times = [], []  # work per window frame; seconds per capture
			self.captures = 0
			self.status_at = time.monotonic()
			print(f"[bridge] {self.renderer_name}; serving frames at tcp://{args.host}:{self.server.port}; window loop and mapper pull are independent", flush=True)

		# --- simulator ---

		def _setup_sim(self):
			import habitat_sim
			self.habitat_sim = habitat_sim
			data = Path(os.environ.get("HABITAT_DATA", str(DEFAULT_DATA))).expanduser()
			dataset = Path(args.scene_dataset).expanduser() if args.scene_dataset else data / "replica_cad" / "replicaCAD.scene_dataset_config.json"
			if not dataset.is_file() and not Path(args.scene).is_file():
				raise SystemExit(f"scene dataset not found: {dataset}\nSet HABITAT_DATA to the folder that holds replica_cad/ or pass --scene-dataset (see README.md).")
			t0 = time.perf_counter()
			sim_cfg = habitat_sim.SimulatorConfiguration()
			if dataset.is_file():
				sim_cfg.scene_dataset_config_file = str(dataset)
			sim_cfg.scene_id = args.scene
			sim_cfg.enable_physics = True  # the furniture of the ReplicaCAD scenes are rigid objects
			sim_cfg.random_seed = args.seed
			sensors = []
			for uuid, kind in (("rgb", habitat_sim.SensorType.COLOR), ("depth", habitat_sim.SensorType.DEPTH)):
				spec = habitat_sim.CameraSensorSpec()
				spec.uuid, spec.sensor_type = uuid, kind
				spec.resolution = [args.height, args.width]
				spec.position = [0.0, args.camera_height, 0.0]
				spec.hfov = args.hfov
				spec.near, spec.far = args.near, args.far
				sensors.append(spec)
			agent_cfg = habitat_sim.agent.AgentConfiguration()
			agent_cfg.radius, agent_cfg.height = args.agent_radius, args.agent_height
			agent_cfg.sensor_specifications = sensors
			spec, actuation = habitat_sim.agent.ActionSpec, habitat_sim.agent.ActuationSpec
			agent_cfg.action_space = {
				name: spec(name, actuation(amount=control.STEP_M if name.startswith("move") else control.STEP_DEG,
					**({"constraint": 60.0} if name.startswith("look") else {})))
				for name in control.ACTIONS.values()
			}
			self.sim = habitat_sim.Simulator(habitat_sim.Configuration(sim_cfg, [agent_cfg]))
			self.sim.seed(args.seed)
			for obj in self.sim.get_rigid_object_manager().get_objects_by_handle_substring().values():
				obj.motion_type = habitat_sim.physics.MotionType.STATIC  # nothing moves when the agent walks into it
			# the navmesh that the agent slides along: furniture counts as an obstacle
			nav = habitat_sim.NavMeshSettings()
			nav.set_defaults()
			nav.agent_radius, nav.agent_height = args.agent_radius, args.agent_height
			nav.include_static_objects = True
			if not self.sim.recompute_navmesh(self.sim.pathfinder, nav):
				raise SystemExit("could not build a navigation mesh for this scene: the agent could not be kept inside it")
			self.agent = self.sim.get_agent(0)
			start = args.start or START.get(args.scene)
			state = habitat_sim.AgentState()
			state.position = list(start) if start else list(self.sim.pathfinder.get_random_navigable_point())
			state.rotation = quaternion.from_rotation_vector([0.0, math.radians(args.start_yaw), 0.0])
			self.sim.initialize_agent(0, state)
			self.start_state = self.agent.get_state()
			self.rgb_sensor = self.agent.scene_node.node_sensor_suite.get("rgb")
			fx = args.width / (2.0 * math.tan(math.radians(args.hfov) / 2.0))
			self.intrinsics = [fx, fx, (args.width - 1) / 2.0, (args.height - 1) / 2.0]
			print(f"[bridge] scene {args.scene}: created in {time.perf_counter() - t0:.1f} s, "
				f"{self.sim.pathfinder.navigable_area:.0f} m2 navigable, start {np.round(self.start_state.position, 2).tolist()}", flush=True)

		def _plan_route(self, x, z, visited):
			"""Autopilot: a walkable route to the candidate place that is farthest from everywhere the agent has been."""
			pathfinder = self.sim.pathfinder
			here = self.agent.get_state().position
			candidates = []
			for _ in range(24):
				point = pathfinder.get_random_navigable_point()
				if np.all(np.isfinite(point)):
					candidates.append((float(point[0]), float(point[2]), point))
			ranked = list(candidates)
			while ranked:
				goal = control.choose_goal([(c[0], c[1]) for c in ranked], visited)
				chosen = next(c for c in ranked if (c[0], c[1]) == goal)
				path = self.habitat_sim.ShortestPath()
				path.requested_start, path.requested_end = here, chosen[2]
				if pathfinder.find_path(path) and len(path.points) >= 2 and math.hypot(goal[0] - x, goal[1] - z) > 1.5:
					return [(float(p[0]), float(p[2])) for p in path.points[1:]]
				ranked.remove(chosen)
			return None

		# --- text over the view ---

		def _setup_hud(self):
			self.hud = None
			if args.no_hud:
				return
			try:
				import string
				from magnum import shaders, text
				font_path = Path(os.environ.get("HABITAT_FONT", "")) if os.environ.get("HABITAT_FONT") else None
				if font_path is None or not font_path.is_file():
					import habitat_sim
					candidates = [Path(habitat_sim.__file__).resolve().parents[i] / "data" / "fonts" / FONT for i in range(1, 6)]
					candidates += [DEFAULT_DATA.parent / "repos" / "habitat-sim" / "data" / "fonts" / FONT]
					font_path = next((c for c in candidates if c.is_file()), None)
				if font_path is None:
					print(f"[bridge] no {FONT} found (set HABITAT_FONT): the view has no text", flush=True)
					return
				font = text.FontManager().load_and_instantiate("TrueTypeFont")
				font.open_file(str(font_path), 14)
				cache = text.GlyphCacheGL(mn.PixelFormat.R8_UNORM, mn.Vector2i(256), mn.Vector2i(1))
				font.fill_glyph_cache(cache, string.printable.strip() + " ")
				renderer = text.Renderer2D(font, cache, 15.0, text.Alignment.TOP_LEFT)
				renderer.reserve(400)
				self.hud = {
					# the renderer only refers to the font and the glyph cache: they must live as long as it does
					"font": font, "shader": shaders.VectorGL2D(), "cache": cache, "renderer": renderer,
					"transform": mn.Matrix3.projection(self.framebuffer_size) @ mn.Matrix3.translation(mn.Vector2(self.framebuffer_size) * mn.Vector2(-0.49, 0.49)),
					"text": None,
				}
			except Exception as error:  # noqa: BLE001 - the text is a convenience
				print(f"[bridge] no text over the view: {error}", flush=True)
				self.hud = None

		def _draw_hud(self, now):
			if not self.hud or not self.hud_on:
				return
			if self.hud["text"] is None or now - self.hud.get("at", 0.0) > 0.25:
				server = self.server
				if server.clients and now - server.last_request < 5.0:
					mapper = f"mapper: {server.mapped} frames, {server.rate:.1f}/s"
				else:
					mapper = "mapper: waiting"
				position = self.agent.get_state().position
				mode = "AUTOPILOT (any move key takes over)" if self.autopilot_on else "manual"
				self.hud["text"] = (
					"W/S walk  A/D strafe  arrows or Q/E turn+look  Shift fast  X stop\n"
					f"R reset  P autopilot  H hide text  Esc quit    [{mode}]\n"
					f"{mapper}   at {position[0]:.1f} {position[2]:.1f}")
				self.hud["at"] = now
				self.hud["renderer"].render(self.hud["text"])
			mn.gl.Renderer.enable(mn.gl.Renderer.Feature.BLENDING)
			mn.gl.Renderer.set_blend_function(mn.gl.Renderer.BlendFunction.ONE, mn.gl.Renderer.BlendFunction.ONE_MINUS_SOURCE_ALPHA)
			shader = self.hud["shader"]
			shader.bind_vector_texture(self.hud["cache"].texture)
			shader.transformation_projection_matrix = self.hud["transform"]
			shader.color = [1.0, 1.0, 0.4]
			shader.draw(self.hud["renderer"].mesh)
			mn.gl.Renderer.disable(mn.gl.Renderer.Feature.BLENDING)

		# --- one frame of the window loop ---

		def draw_event(self):
			tick = time.perf_counter()
			dt = min(tick - self.last_tick, 0.1)
			self.last_tick = tick
			now = time.monotonic()
			if self.quit or self._run_over(now):
				self._shutdown()
				self.exit(0)
				return
			mn.gl.default_framebuffer.clear(mn.gl.FramebufferClear.COLOR | mn.gl.FramebufferClear.DEPTH)
			self._move(dt)
			generation = self.server.poll()
			if generation:
				self._capture(generation)
			else:
				self.sim._sensors["rgb"].draw_observation()
			self.rgb_sensor.render_target.blit_rgba_to_default()
			mn.gl.default_framebuffer.bind()  # the sensor's own framebuffer is bound after its draw; the text goes to the window
			self._draw_hud(now)
			self.swap_buffers()
			if now - self.status_at > 10.0 and self.server.clients:
				self.status_at = now
				print(f"[bridge] {self.captures} frames served, mapper {self.server.rate:.1f}/s", flush=True)
			self.loop_times.append(time.perf_counter() - tick)
			spare = 1.0 / args.fps_limit - (time.perf_counter() - tick)
			if spare > 0:
				time.sleep(spare)
			self.redraw()

		def _move(self, dt):
			# the mapper reads one frame to set up its camera, loads its models, then maps: nothing walks before its third frame
			if self.autopilot_on and not self.keys and self.captures >= MAPPING_FROM_FRAME:
				state = self.agent.get_state()
				motion = self.autopilot.motion(float(state.position[0]), float(state.position[2]),
					control.yaw_from_rotation_matrix(quaternion.as_rotation_matrix(state.rotation)), dt)
			else:
				motion = control.motion_from_keys(self.keys)
			for name, count in self.budget.actions(motion, dt, boost=self.boost):
				for _ in range(count):
					self.agent.act(name)

		def _capture(self, generation):
			t0 = time.perf_counter()
			observations = self.sim.get_sensor_observations()
			rgb = np.ascontiguousarray(observations["rgb"][:, :, :3])
			depth = protocol.encode_depth(observations["depth"], args.far)
			sensor = self.agent.get_state().sensor_states["rgb"]
			pose = np.eye(4)
			pose[:3, :3] = quaternion.as_rotation_matrix(sensor.rotation)
			pose[:3, 3] = sensor.position
			meta = {
				"wall_time": time.time(),
				"intrinsics": self.intrinsics,
				"depth_scale": protocol.DEPTH_SCALE,
				"pose": pose.reshape(-1).tolist(),
				"pose_convention": "opengl_yup",
				"info": {"simulator": "habitat-sim " + getattr(self.habitat_sim, "__version__", "?"), "scene": args.scene,
					"hfov": args.hfov, "camera_height": args.camera_height, "depth_far": args.far, "depth": "simulator ground truth"},
			}
			self.server.publish(generation, meta, rgb, depth)
			self.captures += 1
			self.capture_times.append(time.perf_counter() - t0)
			if self.captures == MAPPING_FROM_FRAME:
				self.first_served = time.monotonic()  # --run-seconds counts from here

		def _run_over(self, now):
			return bool(args.run_seconds and self.first_served and now - self.first_served >= args.run_seconds)

		# --- keyboard ---

		def _logical(self, key):
			K = Application.Key
			names = {"W": "w", "A": "a", "S": "s", "D": "d", "Q": "q", "E": "e", "UP": "up", "DOWN": "down", "LEFT": "left", "RIGHT": "right"}
			for attribute, name in names.items():
				if key == getattr(K, attribute, None):
					return name
			return None

		def key_press_event(self, event):
			K = Application.Key
			key = event.key
			if key == K.ESC:
				self.quit = True
			elif key in (getattr(K, "LEFT_SHIFT", None), getattr(K, "RIGHT_SHIFT", None)):
				self.boost = True
			elif key == K.R:
				self.agent.set_state(self.start_state, reset_sensors=True)
			elif key == K.P and not event.is_repeated:
				self.autopilot_on = not self.autopilot_on
			elif key == K.H and not event.is_repeated:
				self.hud_on = not self.hud_on
			elif key == K.X:
				self.keys.clear()
				self.autopilot_on = False
			else:
				name = self._logical(key)
				if name:
					self.keys.add(name)
					self.autopilot_on = False  # someone is steering
			event.accepted = True

		def key_release_event(self, event):
			K = Application.Key
			if event.key in (getattr(K, "LEFT_SHIFT", None), getattr(K, "RIGHT_SHIFT", None)):
				self.boost = False
			name = self._logical(event.key)
			if name:
				self.keys.discard(name)
			event.accepted = True

		def exit_event(self, event):
			self.quit = True
			self._shutdown()
			event.accepted = True

		def request_quit(self, *_):
			self.quit = True

		def _shutdown(self):
			if getattr(self, "_down", False):
				return
			self._down = True
			self.server.close()
			summary = {
				"renderer": self.renderer_name,
				"window_fps": len(self.loop_times) / max(1e-9, time.perf_counter() - self.started),  # what the window shows (capped by --fps-limit)
				"window_frame_work_ms_mean": 1000 * sum(self.loop_times) / max(1, len(self.loop_times)),  # render + draw + swap, without the sleep
				"frames_served": self.captures,
				"capture_ms_mean": 1000 * sum(self.capture_times) / max(1, len(self.capture_times)),
				"capture_ms_p95": 1000 * percentile(self.capture_times, 0.95),
				"resolution": [args.width, args.height],
			}
			print("[bridge] summary " + json.dumps(summary), flush=True)
			if args.stats_file:
				Path(args.stats_file).write_text(json.dumps(summary, indent=2), encoding="utf-8")
			self.sim.close(destroy=True)

	app = Bridge()
	if args.check:
		generation = 1
		app.server._requests = generation  # one demand, answered below without a client
		app._capture(generation)
		frame = app.server._frame
		print("[bridge] check ok: " + json.dumps({"rgb": list(frame[1].shape), "depth_valid_fraction": float((frame[2] > 0).mean()),
			"depth_max_m": float(frame[2].max()) / protocol.DEPTH_SCALE, "intrinsics": app.intrinsics}), flush=True)
		app._shutdown()
		return 0
	signal.signal(signal.SIGINT, app.request_quit)
	signal.signal(signal.SIGTERM, app.request_quit)
	return app.exec()


def main(argv=None):
	args = parse_args(argv)
	sys.exit(run(args) or 0)


if __name__ == "__main__":
	main()
