import subprocess
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class JustfileTest(unittest.TestCase):
	def _dry_run(self, recipe: str, *args: str) -> subprocess.CompletedProcess:
		return subprocess.run(
			["just", "--dry-run", recipe, *args],
			cwd=REPO,
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
		)

	def test_test_recipe_runs_replay_regression_suite(self) -> None:
		result = self._dry_run("test")
		output = result.stdout + result.stderr
		self.assertEqual(result.returncode, 0, output)
		self.assertIn("tests.test_justfile", output)
		self.assertIn("tests.test_generate_orchestrator_graph", output)
		self.assertIn("tests.test_fetch_tum", output)
		self.assertIn("tests.test_replay_catalog", output)
		self.assertIn("tests.test_description_provenance", output)
		self.assertIn("tests.test_reference_mesh", output)
		self.assertIn("tests.test_replay_reference_mesh", output)
		self.assertIn("tests.test_replay_browser", output)
		self.assertIn("tests.test_replay_rrd", output)
		self.assertIn("tests.test_run_replay", output)
		self.assertIn("tests.test_output_layout", output)
		self.assertIn("tests.test_cluster_places_adapter", output)
		self.assertIn("tests.test_scene_graph_utils", output)
		for module in ("tests.test_replay_manifest", "tests.test_replay_datasets", "tests.test_replay_web_paths", "tests.test_replay_web_runs",
				"tests.test_replay_web_server", "tests.test_replay_artifacts", "tests.test_replay_events", "tests.test_replay_jobs",
				"tests.test_replay_bundle", "tests.test_replay_workspace_browser"):
			self.assertIn(module, output)
		host, container = output.split("docker compose run", 1)
		for module in ("tests.test_replay_web_server", "tests.test_replay_artifacts", "tests.test_replay_events", "tests.test_replay_jobs"):
			self.assertIn(module, host, "the service runs on the WSL host and is tested there: " + module)
		self.assertIn("tests.test_replay_bundle", container.split("powershell", 1)[0], "the finalizer needs the container")
		self.assertIn("RUN_BROWSER_TESTS", output)
		self.assertIn("import selenium", output)
		self.assertIn("browser test skipped", output)

	def test_test_recipe_runs_the_agent_suites_without_asking_a_provider(self) -> None:
		result = self._dry_run("test")
		output = result.stdout + result.stderr
		self.assertEqual(result.returncode, 0, output)
		host, rest = output.split("docker compose run", 1)
		container = rest.split("powershell", 1)[0]
		for module in ("tests.test_replay_history", "tests.test_replay_agent_protocol", "tests.test_replay_agent_client", "tests.test_replay_chat"):
			self.assertIn(module, host, "the service side of the chat is standard library only and is tested on the host: " + module)
		for module in ("tests.test_scene_agent_conversation", "tests.test_replay_query"):
			self.assertIn(module, container, "the Agent and the scene graph loader need the container: " + module)
		for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
			self.assertNotIn(key, output, "the suites use fake providers: no command of the recipe hands over a key")

	def test_the_container_guard_sees_the_replay_agent_worker(self) -> None:
		for recipe in ("tum-post", "coda-post"):
			result = self._dry_run(recipe)
			text = result.stdout + result.stderr
			self.assertEqual(result.returncode, 0, text)
			self.assertIn("agent_worker", text, recipe + ": an idle chat worker holds models on the GPU, so a heavy job started from a terminal waits for it")

	def test_bare_just_keeps_live_preview_as_the_default(self) -> None:
		result = subprocess.run(
			["just", "--dry-run"],
			cwd=REPO,
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
		)
		output = result.stdout + result.stderr
		self.assertEqual(result.returncode, 0, output)
		self.assertIn("scripts/run_live_preview.sh 120 15", output)
		self.assertNotIn("python3 -m unittest", output)

	def test_rerun_web_without_a_path_selects_the_latest_scene_graph(self) -> None:
		result = self._dry_run("rerun-web")
		text = result.stdout + result.stderr
		self.assertEqual(result.returncode, 0, text)
		self.assertIn("find output -type f", text)
		self.assertIn("dsg_updated.json", text)
		self.assertIn("dsg.json", text)

	def test_tum_test_recipe_uses_existing_dataset_without_dam(self) -> None:
		result = self._dry_run("tum-test", "12")
		output = result.stdout + result.stderr
		self.assertEqual(result.returncode, 0, output)
		self.assertIn("NAME=tum_rrd_test", output)
		self.assertIn("workers.num_grounding_workers=0", output)
		self.assertIn("scripts/run_replay.sh 12 15", output)

	def test_tum_test_recipe_defaults_to_thirty_frames(self) -> None:
		result = self._dry_run("tum-test")
		output = result.stdout + result.stderr
		self.assertEqual(result.returncode, 0, output)
		self.assertIn("scripts/run_replay.sh 30 15", output)

	def test_recipes_take_the_tum_sequence_as_an_optional_last_argument(self) -> None:
		for recipe, args, name in (
			("tum-test", ("12", "fr1_room"), "tum_rrd_test_fr1_room"),
			("live-test", ("fr3_walking_xyz",), "tum_live_test_fr3_walking_xyz"),
			("tum-run", ("50", "fr1_room"), "tum_replay_fr1_room"),
		):
			output = self._dry_run(recipe, *args)
			text = output.stdout + output.stderr
			self.assertEqual(output.returncode, 0, text)
			self.assertIn(f"SEQ={args[-1]}", text, recipe)
			self.assertIn(f"NAME={name}", text, recipe)

	def test_default_sequence_keeps_the_original_output_names(self) -> None:
		for recipe, name in (("tum-test", "tum_rrd_test"), ("live-test", "tum_live_test"), ("tum-run", "tum_replay")):
			output = self._dry_run(recipe)
			text = output.stdout + output.stderr
			self.assertEqual(output.returncode, 0, text)
			self.assertIn("SEQ=fr1_desk", text, recipe)
			self.assertRegex(text, rf"NAME={name}(?![\w])", recipe)

	def test_tum_fetch_and_list_recipes_run_the_downloader_in_the_container(self) -> None:
		fetch = self._dry_run("tum-fetch", "fr1_room", "fr3_walking_xyz")
		text = fetch.stdout + fetch.stderr
		self.assertEqual(fetch.returncode, 0, text)
		self.assertIn("https_proxy=", text)
		self.assertIn("scripts/fetch_tum.py get fr1_room fr3_walking_xyz", text)
		listing = self._dry_run("tum-list")
		text = listing.stdout + listing.stderr
		self.assertEqual(listing.returncode, 0, text)
		self.assertIn("scripts/fetch_tum.py list", text)

	def test_tum_postprocess_recipes_target_the_selected_run_root(self) -> None:
		default = self._dry_run("tum-post")
		text = default.stdout + default.stderr
		self.assertEqual(default.returncode, 0, text)
		self.assertIn("-e RESULT_ROOT=output/tum_replay", text)
		self.assertIn("-e CLUSTER_PLACES_CONFIG=config/cluster_places_config_tum.yaml", text)
		self.assertIn("-e PLACES_LAYER=TRAVERSABILITY", text)
		self.assertNotIn("RESULT_ROOT=output/tum_replay_", text)
		self.assertIn("scripts/postprocess_coda.sh", text)

		chosen = self._dry_run("tum-post", "fr1_room", "out_a", "out_b")
		text = chosen.stdout + chosen.stderr
		self.assertEqual(chosen.returncode, 0, text)
		self.assertIn("-e RESULT_ROOT=output/tum_replay_fr1_room", text)
		self.assertIn("-e CLUSTER_PLACES_CONFIG=config/cluster_places_config_tum.yaml", text)
		self.assertIn("-e PLACES_LAYER=TRAVERSABILITY", text)
		self.assertIn("scripts/postprocess_coda.sh out_a out_b", text)

		summarize = self._dry_run("tum-summarize", "fr3_walking_xyz", "out_x")
		text = summarize.stdout + summarize.stderr
		self.assertEqual(summarize.returncode, 0, text)
		self.assertIn("-e RESULT_ROOT=output/tum_replay_fr3_walking_xyz", text)
		self.assertIn("scripts/postprocess_coda.sh --summaries out_x", text)

	def test_tum_run_and_test_pass_the_reference_mesh_policy_into_the_container(self) -> None:
		for recipe, args in (("tum-run", ()), ("tum-run", ("50", "fr1_room")), ("tum-test", ())):
			output = self._dry_run(recipe, *args)
			text = output.stdout + output.stderr
			self.assertEqual(output.returncode, 0, text)
			# `-e NAME` without a value forwards the caller's value, and nothing when it is not set
			self.assertRegex(text, r"-e REFERENCE_MESH(?![\w=])", recipe)
			self.assertIn("-e REFERENCE_MESH_CACHE", text, recipe)

	def test_tum_run_reports_the_new_result_bundle_without_searching_for_a_page(self) -> None:
		for args in ((), ("30", "fr1_room")):
			output = self._dry_run("tum-run", *args)
			text = output.stdout + output.stderr
			self.assertEqual(output.returncode, 0, text)
			self.assertIn("scripts/run_replay.sh", text)
			self.assertNotIn("replay.html", text)
			self.assertIn("-newer", text, "only a result created by this run is reported")
			self.assertIn("replay/manifest.json", text)
			self.assertIn("just replay-web", text)
			self.assertIn("replay.rrd", text)

	def test_the_replay_recipes_describe_bundles_instead_of_pages(self) -> None:
		text = (REPO / "justfile").read_text(encoding="utf-8")
		for recipe in ("tum-test frames=", "tum-run frames="):
			comment = text.split(recipe, 1)[0].rstrip().splitlines()[-1]
			self.assertIn("replay/", comment, recipe)
			self.assertNotIn("replay.html", comment, recipe)

	def test_replay_web_serves_the_workspace_on_loopback(self) -> None:
		result = self._dry_run("replay-web")
		text = result.stdout + result.stderr
		self.assertEqual(result.returncode, 0, text)
		self.assertIn("python3 -m scripts.replay_web.server --host 127.0.0.1 --port 8765", text)
		self.assertIn("http://127.0.0.1:8765/replay.html", text)
		self.assertNotIn("docker compose", text, "the service runs on the WSL host, not in the container")
		other = self._dry_run("replay-web", "localhost", "9000")
		self.assertIn("--host localhost --port 9000", other.stdout + other.stderr)

	def test_old_replay_recipe_is_removed(self) -> None:
		result = self._dry_run("replay")
		self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

	def test_justfile_documents_the_verified_tum_sequences(self) -> None:
		text = (REPO / "justfile").read_text(encoding="utf-8")
		for name in ("fr1_desk", "fr1_desk2", "fr1_room", "fr3_long_office_household", "fr3_walking_xyz"):
			self.assertIn(name, text.split("default: live-test", 1)[0])

	def test_reference_mesh_recipe_builds_the_cache_of_one_dataset(self) -> None:
		default = self._dry_run("reference-mesh")
		text = default.stdout + default.stderr
		self.assertEqual(default.returncode, 0, text)
		self.assertIn("scripts/build_reference_mesh.py /datasets/tum_fr1_desk", text)
		self.assertIn("-e REFERENCE_MESH_CACHE", text)
		self.assertNotIn("--force", text)
		other = self._dry_run("reference-mesh", "fr1_room", "rebuild")
		text = other.stdout + other.stderr
		self.assertEqual(other.returncode, 0, text)
		self.assertIn("scripts/build_reference_mesh.py /datasets/tum_fr1_room --force", text)
		self.assertNotEqual(self._dry_run("reference-mesh", "fr1_desk", "sometimes").returncode, 0)

	def test_orchestrator_graph_recipe_regenerates_offline_documentation(self) -> None:
		result = self._dry_run("orchestrator-graph")
		output = result.stdout + result.stderr
		self.assertEqual(result.returncode, 0, output)
		self.assertIn("scripts/generate_orchestrator_graph.py", output)
		self.assertIn("--source-root src", output)
		self.assertIn("--template scripts/orchestrator_graph_viewer.html", output)
		self.assertIn("--output docs/orchestrator_process_frame.html", output)

	def test_gs_world_recipes_run_in_the_container(self) -> None:
		from scripts.gs_world import env

		install = self._dry_run("gs-install")
		text = install.stdout + install.stderr
		self.assertEqual(install.returncode, 0, text)
		self.assertIn("--no-deps --target /opt/daaam_ws/gs_site", text, "gsplat stays out of the DAAAM virtual environment")
		for package in env.PACKAGES:
			self.assertIn(package, text, "the recipe installs the versions gs_world checks for")
		self.assertIn(f"gsplat=={env.GSPLAT_VERSION}", text)

		train = self._dry_run("gs-train", "fr1_room", "--iterations", "7000")
		text = train.stdout + train.stderr
		self.assertEqual(train.returncode, 0, text)
		self.assertIn("python -m scripts.gs_world.train fr1_room --iterations 7000", text)
		self.assertIn("agent_worker", text, "training waits for the other heavy containers")
		render = self._dry_run("gs-render")
		text = render.stdout + render.stderr
		self.assertEqual(render.returncode, 0, text)
		self.assertIn("python -m scripts.gs_world.render latest", text)

		bag = self._dry_run("gs-bag", "/tmp/rec.bag", "--list")
		text = bag.stdout + bag.stderr
		self.assertEqual(bag.returncode, 0, text)
		self.assertIn(":/bag_in:ro", text, "the bag's folder is mounted read-only")
		self.assertIn("python -m scripts.gs_world.rosbag_to_rgbd", text)
		self.assertIn("--list", text)

		daaam = self._dry_run("gs-daaam", "/data/gs_world/models/tum_fr1_room/t/renders/original")
		text = daaam.stdout + daaam.stderr
		self.assertEqual(daaam.returncode, 0, text)
		self.assertIn("scripts/gs_world/run_daaam.sh /data/gs_world/models/tum_fr1_room/t/renders/original all", text)

		comparison = self._dry_run("gs-compare", "output/tum_replay_fr1_room", "output/gs_tum_fr1_room_original")
		text = comparison.stdout + comparison.stderr
		self.assertEqual(comparison.returncode, 0, text)
		self.assertIn("python -m scripts.gs_world.compare_maps output/tum_replay_fr1_room output/gs_tum_fr1_room_original", text)
		self.assertNotEqual(self._dry_run("gs-compare").returncode, 0, "at least the reference is required")

		for recipe in ("coda-post", "gs-daaam"):
			args = ("x",) if recipe == "gs-daaam" else ()
			result = self._dry_run(recipe, *args)
			self.assertIn("scripts.gs_world.train", result.stdout + result.stderr,
				recipe + ": a pipeline does not start next to a 3DGS training")

	def test_test_recipe_runs_the_gs_world_suites_in_the_container(self) -> None:
		result = self._dry_run("test")
		output = result.stdout + result.stderr
		container = output.split("docker compose run", 1)[1].split("powershell", 1)[0]
		for module in ("tests.test_gs_world_data", "tests.test_gs_world_model", "tests.test_gs_world_rosbag"):
			self.assertIn(module, container, "the gs_world suites need numpy, OpenCV and torch from the container")

	def test_habitat_live_recipes_run_the_host_script_that_starts_the_bridge_and_the_mapper(self) -> None:
		live = self._dry_run("habitat-live")
		text = live.stdout + live.stderr
		self.assertEqual(live.returncode, 0, text)
		self.assertIn("bash scripts/habitat_live.sh all", text)
		self.assertIn("run_habitat_live", text, "the container guard sees the mapper of a live session")
		limited = self._dry_run("habitat-live", "300")
		self.assertIn("bash scripts/habitat_live.sh 300", limited.stdout + limited.stderr)

		check = self._dry_run("habitat-live-test", "90")
		text = check.stdout + check.stderr
		self.assertEqual(check.returncode, 0, text)
		self.assertIn("AUTOPILOT=1 RUN_SECONDS=90 KEEP_VIEWER=0 REPLAY=1 NAME=habitat_live_test bash scripts/habitat_live.sh", text)
		self.assertIn("run_habitat_live", text, "the test run is guarded as well")

		bridge = self._dry_run("habitat-bridge", "--speed", "0.9")
		text = bridge.stdout + bridge.stderr
		self.assertEqual(bridge.returncode, 0, text)
		self.assertIn("bash scripts/habitat_live.sh --bridge-only --speed 0.9", text)

	def test_test_recipe_runs_the_habitat_live_suites(self) -> None:
		result = self._dry_run("test")
		output = result.stdout + result.stderr
		self.assertEqual(result.returncode, 0, output)
		host, rest = output.split("docker compose run", 1)
		container = rest.split("powershell", 1)[0]
		for module in ("tests.test_live_protocol", "tests.test_habitat_live_control", "tests.test_habitat_live_scripts"):
			self.assertIn(module, host, "standard library and numpy only, tested on the WSL host: " + module)
		self.assertIn("tests.test_live_dataset", container, "the live dataset needs OpenCV and SciPy from the container")

if __name__ == "__main__":
	unittest.main()
