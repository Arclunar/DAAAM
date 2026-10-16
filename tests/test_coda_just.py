import subprocess
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
GUARDED = (
	("coda-depth",),
	("coda-run",),
	("coda-chunks",),
	("coda-post",),
	("coda-summarize",),
	("coda-ask", "a question", "out_x"),
)
UNGUARDED = (
	("coda-download",),
	("coda-bag",),
	("coda-list",),
	("coda-view", "out_x"),
)


class CodaJustfileTest(unittest.TestCase):
	def _dry_run(self, recipe: str, *args: str) -> str:
		result = subprocess.run(
			["just", "--dry-run", recipe, *args],
			cwd=REPO,
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
		)
		output = result.stdout + result.stderr
		self.assertEqual(result.returncode, 0, output)
		return output

	def test_the_coda_recipes_are_listed_and_the_guard_is_hidden(self) -> None:
		result = subprocess.run(["just", "--list"], cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace")
		self.assertEqual(result.returncode, 0, result.stderr)
		for name in ("coda-download", "coda-depth", "coda-bag", "coda-run", "coda-chunks", "coda-post", "coda-summarize", "coda-list", "coda-ask", "coda-view"):
			self.assertRegex(result.stdout, rf"(?m)^\s+{name}\b", name)
		self.assertNotIn("_coda-guard", result.stdout)

	def test_download_takes_the_first_frames_through_the_proxy_in_the_container(self) -> None:
		output = self._dry_run("coda-download")
		self.assertIn("daaam-ws python scripts/download_coda_subset.py -d /data/CODa -se 0 --frames 1000 --proxy http", output)
		self.assertIn("--frames 0 ", self._dry_run("coda-download", "0") + " ")

	def test_depth_runs_the_foundation_stereo_wrapper_on_the_container_data_root(self) -> None:
		output = self._dry_run("coda-depth")
		self.assertIn("scripts/run_foundation_stereo_coda.py --dataset_folder \"$CODA_ROOT_DIR\" --sequence_id 0", output)
		self.assertIn("https_proxy=", output)

	def test_bag_defaults_to_the_whole_sequence_at_the_original_rate(self) -> None:
		self.assertIn("scripts/make_coda_bag.sh 0 0 coda_0_with_depth", self._dry_run("coda-bag"))
		self.assertIn("scripts/make_coda_bag.sh 2053 2053 mychunk", self._dry_run("coda-bag", "2053", "2053", "mychunk"))

	def test_run_plays_at_a_chosen_speed_and_takes_a_bag_name(self) -> None:
		default = self._dry_run("coda-run")
		self.assertIn("-e RATE=0.3 -e LABELS=5000", default)
		self.assertIn("bash scripts/run_coda_example.sh", default)
		chosen = self._dry_run("coda-run", "coda_0_chunk2of4_x.bag", "0.5", "1000")
		self.assertIn("-e RATE=0.5 -e LABELS=1000", chosen)
		self.assertIn("scripts/run_coda_example.sh coda_0_chunk2of4_x.bag", chosen)

	def test_chunks_take_the_count_the_speed_and_the_chunks_to_run(self) -> None:
		self.assertIn("-e RATE=0.3 daaam-ws bash scripts/run_coda_chunks.sh 4", self._dry_run("coda-chunks"))
		chosen = self._dry_run("coda-chunks", "5", "0.4", "2", "3")
		self.assertIn("-e RATE=0.4", chosen)
		self.assertIn("scripts/run_coda_chunks.sh 5 2 3", chosen)

	def test_post_summarize_and_list_share_one_script(self) -> None:
		self.assertIn("scripts/postprocess_coda.sh out_a out_b", self._dry_run("coda-post", "out_a", "out_b"))
		summarize = self._dry_run("coda-summarize", "out_a")
		self.assertIn("scripts/postprocess_coda.sh --summaries out_a", summarize)
		self.assertIn("scripts/postprocess_coda.sh --list", self._dry_run("coda-list"))
		self.assertNotIn("--summaries", self._dry_run("coda-post"))

	def test_ask_quotes_the_question_and_uses_the_summarised_scene_graph(self) -> None:
		output = self._dry_run("coda-ask", "这里有垃圾箱吗? it's 'x'", "out_20261016_021046")
		self.assertIn("printf '%s\\n\\n' ", output)
		self.assertIn("scripts/demo_query.py --dsg-path output/coda/out_20261016_021046/clustered_dsg_with_summaries.json --seq-id 0 --model-name gpt-5-mini", output)
		self.assertIn("-e HF_HOME=/data/hf_cache", output)
		self.assertIn("'\\''", output, "single quotes inside the question must be escaped for the shell")

	def test_view_hands_the_scene_graph_to_rerun_web(self) -> None:
		self.assertIn("rerun-web output/coda/out_x/clustered_dsg_with_summaries.json", self._dry_run("coda-view", "out_x"))

	def test_heavy_recipes_refuse_to_start_next_to_another_pipeline_container(self) -> None:
		for recipe, *args in GUARDED:
			output = self._dry_run(recipe, *args)
			self.assertIn("docker ps --no-trunc --format '{{.Command}}'", output, recipe)
			self.assertIn("run_coda_example", output, recipe)
			self.assertLess(output.index("docker ps"), output.index("docker compose run"), recipe)
		for recipe, *args in UNGUARDED:
			self.assertNotIn("docker ps", self._dry_run(recipe, *args), recipe)


if __name__ == "__main__":
	unittest.main()
