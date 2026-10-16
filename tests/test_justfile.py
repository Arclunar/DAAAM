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
		self.assertIn("RUN_BROWSER_TESTS", output)
		self.assertIn("import selenium", output)
		self.assertIn("browser test skipped", output)

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
		self.assertIn("-e PLACES_LAYER=PLACES", text)
		self.assertNotIn("RESULT_ROOT=output/tum_replay_", text)
		self.assertIn("scripts/postprocess_coda.sh", text)

		chosen = self._dry_run("tum-post", "fr1_room", "out_a", "out_b")
		text = chosen.stdout + chosen.stderr
		self.assertEqual(chosen.returncode, 0, text)
		self.assertIn("-e RESULT_ROOT=output/tum_replay_fr1_room", text)
		self.assertIn("-e CLUSTER_PLACES_CONFIG=config/cluster_places_config_tum.yaml", text)
		self.assertIn("-e PLACES_LAYER=PLACES", text)
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

if __name__ == "__main__":
	unittest.main()
