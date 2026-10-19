import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG = "config/hydra_config/habitat_house_khronos.yaml"


def free_port() -> int:
	probe = socket.socket()
	probe.bind(("127.0.0.1", 0))
	port = probe.getsockname()[1]
	probe.close()
	return port


class ScriptTest(unittest.TestCase):
	"""The two shell scripts run in a scratch repository against fake python, docker and ros2 commands."""

	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name)
		(self.root / "scripts" / "habitat_live").mkdir(parents=True)
		(self.root / "fakebin").mkdir()
		(self.root / "config" / "hydra_config").mkdir(parents=True)
		(self.root / CONFIG).write_text("---\n", encoding="utf-8")
		self.calls = self.root / "calls.log"
		for name in ("habitat_live.sh", "run_habitat_live.sh"):
			shutil.copy(REPO / "scripts" / name, self.root / "scripts" / name)
		self.processes = []
		self.addCleanup(self.stop_processes)

	def stop_processes(self) -> None:
		for process in self.processes:
			if process.poll() is None:
				process.kill()
				process.wait()

	def write_executable(self, path: Path, body: str) -> None:
		path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
		path.chmod(path.stat().st_mode | stat.S_IXUSR)

	def env(self, **extra: str) -> dict:
		env = {key: value for key, value in os.environ.items() if key not in ("FPS", "NAME", "DAM", "RECORD", "VIEWER", "KEEP_VIEWER", "REPLAY", "LIVE_URL", "HYDRA_CONFIG", "ZMQ_URL", "SCENE", "AUTOPILOT", "RUN_SECONDS", "BRIDGE_ARGS", "BRIDGE_PORT", "DEPTH_LB", "DEPTH_UB", "LABELS", "EXTRA_OVERRIDES", "REPLAY_EVERY", "HABITAT_DATA", "VISUALIZER_CONFIG", "RVIZ_CONFIG")}
		env.update(PATH=f"{self.root / 'fakebin'}:{env['PATH']}", CALLS=str(self.calls), DISPLAY=":0")
		env.update(extra)
		return env

	def calls_text(self) -> str:
		return self.calls.read_text(encoding="utf-8") if self.calls.exists() else ""

	def call(self, prefix: str) -> str:
		lines = [line for line in self.calls_text().splitlines() if line.startswith(prefix)]
		self.assertTrue(lines, f"no call starting with {prefix!r} in\n{self.calls_text()}")
		return lines[0]


class HostScriptTest(ScriptTest):
	def setUp(self) -> None:
		super().setUp()
		self.port = free_port()
		self.data = self.root / "data"
		(self.data / "replica_cad").mkdir(parents=True)
		(self.data / "replica_cad" / "replicaCAD.scene_dataset_config.json").write_text("{}", encoding="utf-8")
		# stands in for the habitat-sim python: records what it is asked, serves the port until it is terminated
		self.python = self.root / "fake_habitat_python"
		self.write_executable(self.python, f"""
			#!{sys.executable}
			import os, signal, socket, sys, time
			args = sys.argv[1:]
			with open(os.environ["CALLS"], "a") as log:
				log.write("bridge " + " ".join(args) + "\\n")
				log.write("env GALLIUM_DRIVER=" + os.environ.get("GALLIUM_DRIVER", "") + " HABITAT_DATA=" + os.environ.get("HABITAT_DATA", "") + "\\n")
			if os.environ.get("FAKE_BRIDGE_DIES"):
				print("[bridge] no luck", flush=True)
				sys.exit(5)
			if os.environ.get("FAKE_BRIDGE_QUICK"):
				sys.exit(0)
			port = int(args[args.index("--port") + 1])
			server = socket.create_server(("127.0.0.1", port))
			server.settimeout(0.2)
			done = []
			signal.signal(signal.SIGTERM, lambda *_: done.append(1))
			print("[bridge] fake bridge is serving", flush=True)
			while not done:
				try:
					server.accept()[0].close()
				except socket.timeout:
					pass
			with open(os.environ["CALLS"], "a") as log:
				log.write("bridge terminated\\n")
			open(os.environ["BRIDGE_DONE"], "w").close()
			""")
		# stands in for docker: `compose run` is the mapper, which ends when the bridge is gone if FAKE_MAPPER_WAITS is set
		self.write_executable(self.root / "fakebin" / "docker", """
			#!/usr/bin/env bash
			echo "docker $*" >> "$CALLS"
			if [ "$1" = stop ]; then exit 0; fi
			if [ -n "${FAKE_MAPPER_WAITS:-}" ]; then
			  for _ in $(seq 1 150); do [ -e "$BRIDGE_DONE" ] && break; /bin/sleep 0.1; done
			fi
			exit "${FAKE_DOCKER_RC:-0}"
			""")
		self.done = self.root / "bridge_done"

	def settings(self, **env: str) -> dict:
		settings = {"HABITAT_PYTHON": str(self.python), "HABITAT_DATA": str(self.data), "BRIDGE_PORT": str(self.port), "BRIDGE_DONE": str(self.done)}
		settings.update(env)
		return self.env(**settings)

	def run_script(self, *args: str, **env: str) -> subprocess.CompletedProcess:
		return subprocess.run(
			["bash", "scripts/habitat_live.sh", *args], cwd=self.root, env=self.settings(**env),
			capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
		)

	def test_a_missing_habitat_environment_is_explained(self) -> None:
		result = self.run_script(HABITAT_PYTHON="/no/such/python")
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertIn("HABITAT_PYTHON", result.stderr)
		self.assertIn("USAGE.md 9.8", result.stderr)
		self.assertEqual(self.calls_text(), "")

	def test_missing_scene_data_is_explained(self) -> None:
		result = self.run_script(HABITAT_DATA=str(self.root / "nothing"))
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertIn("ReplicaCAD", result.stderr)
		self.assertEqual(self.calls_text(), "")

	def test_another_scene_does_not_need_replicacad(self) -> None:
		result = self.run_script(HABITAT_DATA=str(self.root / "nothing"), SCENE="my_scene.glb", FAKE_BRIDGE_QUICK="1", FAKE_MAPPER_WAITS="")
		self.assertNotIn("ReplicaCAD", result.stderr)

	def test_no_display_is_explained(self) -> None:
		result = self.run_script(DISPLAY="")
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertIn("DISPLAY", result.stderr)

	def test_a_port_in_use_is_refused(self) -> None:
		server = socket.create_server(("127.0.0.1", self.port))
		self.addCleanup(server.close)
		result = self.run_script()
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertIn("already in use", result.stderr)
		self.assertEqual(self.calls_text(), "")

	def test_the_bridge_and_the_mapper_run_together_and_the_bridge_is_stopped_afterwards(self) -> None:
		result = self.run_script()
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		bridge = self.call("bridge ")
		self.assertIn(f"--port {self.port} --scene apt_1", bridge)
		self.assertNotIn("--autopilot", bridge)
		self.assertIn("GALLIUM_DRIVER=d3d12", self.call("env "), "the GPU renderer is selected for the bridge only")
		self.assertIn(f"HABITAT_DATA={self.data}", self.call("env "))
		docker = self.call("docker compose")
		self.assertIn("run --rm -T --name daaam-habitat-live-", docker)
		self.assertIn(f"-e LIVE_URL=tcp://127.0.0.1:{self.port}", docker)
		self.assertIn("-e NAME=habitat_live", docker)
		self.assertIn("daaam-ws bash scripts/run_habitat_live.sh", docker)
		self.assertTrue(self.calls_text().rstrip().endswith("bridge terminated"), "the bridge is stopped after the mapper: " + self.calls_text())
		self.assertIn("The Habitat window is open", result.stdout)
		self.assertIn("[bridge] fake bridge is serving", result.stdout)

	def test_settings_reach_the_container_only_when_they_are_set(self) -> None:
		result = self.run_script("50", FPS="2", DAM="1", REPLAY="1")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		docker = self.call("docker compose")
		for variable in ("FPS", "DAM", "REPLAY"):
			self.assertRegex(docker, rf"-e {variable}( |$)")
		for variable in ("KEEP_VIEWER", "VIEWER", "HYDRA_CONFIG", "RECORD"):
			self.assertNotRegex(docker, rf"-e {variable}( |$)")
		self.assertTrue(docker.endswith("scripts/run_habitat_live.sh 50"), "the frame limit is the mapper's argument: " + docker)

	def test_autopilot_run_seconds_and_more_arguments_go_to_the_bridge(self) -> None:
		result = self.run_script(AUTOPILOT="1", RUN_SECONDS="30", BRIDGE_ARGS="--speed 0.8 --start-yaw 90", SCENE="apt_1")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		bridge = self.call("bridge ")
		for argument in ("--autopilot", "--run-seconds 30", "--speed 0.8", "--start-yaw 90"):
			self.assertIn(argument, bridge)

	def test_the_exit_code_of_the_mapper_is_the_exit_code(self) -> None:
		result = self.run_script(FAKE_DOCKER_RC="3")
		self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
		self.assertIn("bridge terminated", self.calls_text())

	def test_a_bridge_that_dies_at_the_start_is_reported_with_its_log_and_no_mapper_starts(self) -> None:
		result = self.run_script(FAKE_BRIDGE_DIES="1")
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertIn("exited before it served frames", result.stderr)
		self.assertNotIn("docker compose", self.calls_text())

	def test_bridge_only_runs_the_window_in_the_foreground(self) -> None:
		result = self.run_script("--bridge-only", "--speed", "0.9", FAKE_BRIDGE_QUICK="1")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		bridge = self.call("bridge ")
		self.assertIn("--speed 0.9", bridge)
		self.assertIn(f"--port {self.port}", bridge)
		self.assertNotIn("docker", self.calls_text())

	def test_ctrl_c_closes_the_simulator_and_the_mapper_finishes_by_itself(self) -> None:
		process = subprocess.Popen(
			["bash", "scripts/habitat_live.sh"], cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
			env=self.settings(FAKE_MAPPER_WAITS="1"),
		)
		self.processes.append(process)
		deadline = time.monotonic() + 30
		while "docker compose" not in self.calls_text() and time.monotonic() < deadline:
			time.sleep(0.1)
		self.assertIn("docker compose", self.calls_text(), "the mapper never started")
		os.kill(process.pid, signal.SIGINT)
		output, _ = process.communicate(timeout=30)
		self.assertEqual(process.returncode, 0, output)
		self.assertIn("closing the simulator", output)
		self.assertIn("bridge terminated", self.calls_text())


class MapperScriptTest(ScriptTest):
	def setUp(self) -> None:
		super().setUp()
		self.zmq_url = f"tcp://127.0.0.1:{free_port()}"  # the real 8001 may be served by a run that is going on right now
		# python: records its arguments; the pipeline "result" is a directory with a scene graph, a session is recorded unless told not to
		self.write_executable(self.root / "fakebin" / "python", """
			#!/usr/bin/env bash
			echo "python $*" >> "$CALLS"
			case "$1" in
			  scripts/run_pipeline.py)
			    name=""; record=1; previous=""
			    for argument in "$@"; do
			      [ "$previous" = "--dataset-name" ] && name="$argument"
			      [ "$argument" = "--no-live-record" ] && record=0
			      previous="$argument"
			    done
			    /bin/sleep 0.05  # the result must be newer than the script's marker file, whose time stamp has a coarse resolution
			    if [ "$record" = 1 ]; then mkdir -p "$2/rgb"; fi
			    mkdir -p "output/$name/out_20260101_000000"
			    echo '{}' > "output/$name/out_20260101_000000/dsg.json"
			    exit "${FAKE_PIPELINE_RC:-0}" ;;
			  scripts/replay.py)
			    if [ "$2" = record ]; then trap 'exit 0' TERM; /bin/sleep 30 & wait $!; fi
			    exit "${FAKE_REPLAY_RC:-0}" ;;
			esac
			exit 0
			""")
		self.write_executable(self.root / "fakebin" / "ros2", """
			#!/usr/bin/env bash
			echo "ros2 $*" >> "$CALLS"
			exit 0
			""")
		self.write_executable(self.root / "fakebin" / "sleep", "#!/bin/sh\nexit 0\n")

	def run_script(self, *args: str, **env: str) -> subprocess.CompletedProcess:
		defaults = {"VIEWER": "0", "ZMQ_URL": self.zmq_url}
		defaults.update(env)
		return subprocess.run(
			["bash", "scripts/run_habitat_live.sh", *args], cwd=self.root, env=self.env(**defaults),
			capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
		)

	def pipeline(self) -> str:
		return self.call("python scripts/run_pipeline.py")

	def test_the_pipeline_maps_the_live_dataset_with_the_house_config(self) -> None:
		result = self.run_script()
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		pipeline = self.pipeline()
		self.assertRegex(pipeline, r"run_pipeline\.py output/habitat_live/session_\d{8}_\d{6} ")
		for expected in ("--dataset-type LiveDataset", "--live-url tcp://127.0.0.1:8002", f"--hydra-config-path {CONFIG}", "--fps 3 ", "--depth-lb 0.3 --depth-ub 5.0",
				f"--zmq-url {self.zmq_url}", "--dataset-name habitat_live", "--no-throttle", "--no-progress", "workers.num_grounding_workers=0", "--sam-model fastsam/FastSAM-x.pt"):
			self.assertIn(expected, pipeline)
		for unexpected in ("--max-frames", "--no-live-record"):
			self.assertNotIn(unexpected, pipeline)

	def test_the_settings_are_honoured(self) -> None:
		result = self.run_script("40", FPS="2", LIVE_URL="tcp://127.0.0.1:9", NAME="mine", DAM="1", RECORD="0", DEPTH_UB="6", EXTRA_OVERRIDES="a.b=1", HYDRA_CONFIG=CONFIG, LABELS="1000")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		pipeline = self.pipeline()
		for expected in ("--max-frames 40", "--fps 2 ", "--live-url tcp://127.0.0.1:9", "--dataset-name mine", "--no-live-record", "--depth-ub 6", "a.b=1", "labels_pseudo.n1000.yaml"):
			self.assertIn(expected, pipeline)
		self.assertNotIn("num_grounding_workers", pipeline, "DAM=1 keeps the grounding workers")

	def test_the_result_is_finished_with_the_recorded_session(self) -> None:
		result = self.run_script()
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		lift = self.call("python scripts/lift_native_places.py")
		self.assertRegex(lift, r"--run output/habitat_live/out_20260101_000000 --dataset output/habitat_live/session_\d{8}_\d{6}$")
		self.assertIn("python scripts/summarize_run.py output/habitat_live/out_20260101_000000", self.calls_text())
		self.assertIn("Recorded session: output/habitat_live/session_", result.stdout)

	def test_without_a_recording_there_is_nothing_to_lift_from(self) -> None:
		result = self.run_script(RECORD="0")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertNotIn("lift_native_places", self.calls_text())
		self.assertIn("summarize_run.py", self.calls_text(), result.stdout + result.stderr)

	def test_a_failed_pipeline_is_not_lifted_and_its_exit_code_is_kept(self) -> None:
		result = self.run_script(FAKE_PIPELINE_RC="4")
		self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
		self.assertNotIn("lift_native_places", self.calls_text())

	def test_replay_mode_records_the_scene_graph_and_finalizes_the_bundle(self) -> None:
		result = self.run_script(REPLAY="1")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertIn(f"python scripts/replay.py record --zmq-url {self.zmq_url} --out output/habitat_live/.replay-recording --every 5", self.calls_text())
		finalize = self.call("python scripts/replay.py finalize")
		for expected in ("--dataset-kind tum --sequence habitat_live", "--run output/habitat_live/out_20260101_000000", "--fps 3",
				"--out output/habitat_live/out_20260101_000000/replay", "--dataset output/habitat_live/session_"):
			self.assertIn(expected, finalize)
		self.assertIn("python scripts/replay.py rrd output/habitat_live/.replay-recording", self.calls_text())
		self.assertFalse((self.root / "output" / "habitat_live" / ".replay-recording").exists(), "the raw recording is removed after a good finalize")
		self.assertIn("just replay-web", result.stdout)

	def test_the_viewer_opens_and_stays_only_when_asked(self) -> None:
		if not Path("/tmp/.X11-unix").is_dir():
			self.skipTest("no X11 socket directory")
		result = self.run_script(VIEWER="1", KEEP_VIEWER="1")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertIn(f"ros2 launch hydra_visualizer streaming_visualizer.launch.yaml use_zmq:=true zmq_url:={self.zmq_url}", self.calls_text())
		self.assertIn("The viewer stays open", result.stdout)
		self.assertNotIn("visualizer_config_path", self.calls_text(), "without the files the package's own defaults are used")
		self.calls.write_text("")
		result = self.run_script(VIEWER="1", KEEP_VIEWER="0")
		self.assertNotIn("The viewer stays open", result.stdout)

	def test_the_house_scale_layer_spacing_and_rviz_layout_are_handed_to_the_viewer(self) -> None:
		if not Path("/tmp/.X11-unix").is_dir():
			self.skipTest("no X11 socket directory")
		for name in ("habitat_visualizer.yaml", "habitat_live.rviz"):
			(self.root / "config" / "hydra_config" / name).write_text("", encoding="utf-8")
		result = self.run_script(VIEWER="1", KEEP_VIEWER="0")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		launch = self.call("ros2 launch")
		config = os.path.realpath(self.root / "config" / "hydra_config")
		self.assertIn(f"visualizer_config_path:={config}/habitat_visualizer.yaml", launch)
		self.assertIn(f"rviz_path:={config}/habitat_live.rviz", launch)
		self.calls.write_text("")
		self.run_script(VIEWER="1", KEEP_VIEWER="0", VISUALIZER_CONFIG="config/hydra_config/missing.yaml")
		self.assertNotIn("visualizer_config_path", self.call("ros2 launch"), "a missing file means the default")
		self.assertIn("rviz_path:=", self.call("ros2 launch"))

	def test_a_scene_graph_address_that_is_taken_is_reported_before_any_model_is_loaded(self) -> None:
		server = socket.create_server(("127.0.0.1", int(self.zmq_url.rsplit(":", 1)[1])))
		self.addCleanup(server.close)
		result = self.run_script()
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertIn("already served by another process", result.stderr)
		self.assertNotIn("run_pipeline.py", self.calls_text())

	def test_no_display_and_a_missing_config_are_explained(self) -> None:
		result = self.run_script(VIEWER="1", DISPLAY="")
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertIn("No X display", result.stderr)
		result = self.run_script(HYDRA_CONFIG="config/nothing.yaml")
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertIn("not found", result.stderr)
		self.assertNotIn("run_pipeline.py", self.calls_text())


if __name__ == "__main__":
	unittest.main()
