import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = ("make_coda_bag.sh", "run_coda_chunks.sh", "postprocess_coda.sh", "run_coda_example.sh")


class CodaScriptTest(unittest.TestCase):
	"""The CODa shell scripts run against fake ros2 / python / pipeline commands in a scratch repository."""

	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.root = Path(self.temp.name)
		(self.root / "scripts").mkdir()
		(self.root / "fakebin").mkdir()
		self.coda = self.root / "data" / "CODa"
		self.coda.mkdir(parents=True)
		self.calls = self.root / "calls.log"
		for name in SCRIPTS:
			shutil.copy(REPO / "scripts" / name, self.root / "scripts" / name)

	def tearDown(self) -> None:
		self.temp.cleanup()

	def _write_executable(self, path: Path, body: str) -> None:
		path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
		path.chmod(path.stat().st_mode | stat.S_IXUSR)

	def _fake_ros2(self) -> None:
		"""ros2 launch ...dataloader...: records the arguments and closes a bag named after bag_path with a time stamp."""
		self._write_executable(
			self.root / "fakebin" / "ros2",
			"""
			#!/usr/bin/env bash
			echo "ros2 $*" >> "$CALLS"
			for a in "$@"; do case "$a" in bag_path:=*) bag="${a#bag_path:=}";; esac; done
			if [ -n "${bag:-}" ]; then
			  if [ "${FAKE_BAG_FAILS:-0}" = 1 ]; then exit 3; fi
			  dir="${bag%.bag}_20260101_00000${FAKE_STAMP:-1}.bag"
			  mkdir -p "$dir" && : > "$dir/metadata.yaml"
			fi
			exit 0
			""",
		)

	def _env(self, **extra: str) -> dict:
		env = dict(os.environ)
		env.update(
			PATH=f"{self.root / 'fakebin'}:{env['PATH']}",
			CODA_ROOT_DIR=str(self.coda),
			CALLS=str(self.calls),
		)
		env.update(extra)
		return env

	def _run(self, script: str, *args: str, **env: str) -> subprocess.CompletedProcess:
		return subprocess.run(
			["bash", f"scripts/{script}", *args],
			cwd=self.root,
			env=self._env(**env),
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
		)

	def _calls(self) -> str:
		return self.calls.read_text(encoding="utf-8") if self.calls.exists() else ""


class MakeCodaBagTest(CodaScriptTest):
	def setUp(self) -> None:
		super().setUp()
		self._fake_ros2()

	def test_the_whole_sequence_has_no_end_frame(self) -> None:
		result = self._run("make_coda_bag.sh")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertIn("start_frame:=0 end_frame:=-1", self._calls())
		self.assertIn("bag_path:=" + str(self.coda / "coda_0_with_depth.bag"), self._calls())
		self.assertIn("depth_source:=3d_raw_estimated", self._calls())
		self.assertIn("Bag: " + str(self.coda / "coda_0_with_depth_20260101_000001.bag"), result.stdout)

	def test_a_range_from_frame_zero_ends_at_its_length(self) -> None:
		self._run("make_coda_bag.sh", "0", "1000")
		self.assertIn("start_frame:=0 end_frame:=1000", self._calls())

	def test_the_loader_quirk_end_frame_is_twice_the_start_plus_the_length(self) -> None:
		# the ROS dataloader returns start_frame .. end_frame-start_frame-1
		for start, length, end in ((2053, 2053, 6159), (4106, 2053, 10265), (6159, 2054, 14372)):
			self.calls.write_text("")
			result = self._run("make_coda_bag.sh", str(start), str(length), f"chunk{start}")
			self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
			self.assertIn(f"start_frame:={start} end_frame:={end}", self._calls())

	def test_a_start_without_a_length_goes_to_the_end(self) -> None:
		self._run("make_coda_bag.sh", "500", "0", "tail")
		self.assertIn("start_frame:=500 end_frame:=-1", self._calls())

	def test_bad_arguments_are_rejected_before_anything_runs(self) -> None:
		for args in (("-1",), ("0", "x"), ("0", "10", "a/b")):
			result = self._run("make_coda_bag.sh", *args)
			self.assertEqual(result.returncode, 2, (args, result.stdout + result.stderr))
		self.assertEqual(self._calls(), "")

	def test_an_incomplete_bag_is_an_error_and_an_older_bag_of_the_same_name_is_not_mistaken_for_it(self) -> None:
		old = self.coda / "coda_0_with_depth_20250101_000000.bag"
		old.mkdir()
		(old / "metadata.yaml").write_text("", encoding="utf-8")
		result = self._run("make_coda_bag.sh", FAKE_BAG_FAILS="1")
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertIn("No complete bag", result.stderr)
		self.assertNotIn("Bag:", result.stdout)


class RunCodaChunksTest(CodaScriptTest):
	def setUp(self) -> None:
		super().setUp()
		depth = self.coda / "3d_raw_estimated" / "cam0" / "0"
		depth.mkdir(parents=True)
		for index in range(10):
			(depth / f"depth_cam0_0_{index}.png").write_bytes(b"")
		# make_coda_bag.sh: records its arguments and closes a bag called <NAME>_<stamp>.bag
		self._write_executable(
			self.root / "scripts" / "make_coda_bag.sh",
			"""
			#!/usr/bin/env bash
			echo "bag $*" >> "$CALLS"
			dir="$CODA_ROOT_DIR/$3_20260101_000001.bag"
			if [ "${FAKE_NO_BAG:-}" != "$3" ]; then mkdir -p "$dir" && : > "$dir/metadata.yaml"; fi
			""",
		)
		# run_coda_example.sh: records its environment, writes a launch.log with 5 processed frames and names the result
		self._write_executable(
			self.root / "scripts" / "run_coda_example.sh",
			"""
			#!/usr/bin/env bash
			echo "run bag=$1 RATE=$RATE LABELS=$LABELS OUT=$OUT" >> "$CALLS"
			mkdir -p "$OUT"
			for i in 1 2 3 4 5; do echo "[x] daaam_node: process_frame - execution time: 1.0 ms" >> "$OUT/launch.log"; done
			echo "Launch finished (exit code 0). Results: output/coda/out_$(basename "$1" .bag)"
			exit "${FAKE_RUN_RC:-0}"
			""",
		)
		self.out_root = self.root / "data" / "output"

	def test_frames_are_split_evenly_and_the_last_chunk_takes_the_remainder(self) -> None:
		result = self._run("run_coda_chunks.sh", "3")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		calls = self._calls()
		self.assertIn("bag 0 3 coda_0_f0-3\n", calls)
		self.assertIn("bag 3 3 coda_0_f3-6\n", calls)
		self.assertIn("bag 6 4 coda_0_f6-10\n", calls)

	def test_every_chunk_runs_with_its_own_bag_log_directory_rate_and_labels(self) -> None:
		self._run("run_coda_chunks.sh", "2", RATE="0.5")
		calls = self._calls()
		self.assertIn(f"run bag={self.coda / 'coda_0_f0-5_20260101_000001.bag'} RATE=0.5 LABELS=5000 OUT={self.out_root / 'coda_run_chunk1of2'}", calls)
		self.assertIn(f"OUT={self.out_root / 'coda_run_chunk2of2'}", calls)

	def test_four_or_more_chunks_use_the_small_label_space(self) -> None:
		self._run("run_coda_chunks.sh", "4")
		self.assertIn("LABELS=1000", self._calls())
		self.assertIn("RATE=0.3", self._calls())

	def test_only_the_wanted_chunks_run(self) -> None:
		result = self._run("run_coda_chunks.sh", "4", "2", "4")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		calls = self._calls()
		self.assertNotIn("chunk1of4", calls)
		self.assertIn("chunk2of4", calls)
		self.assertNotIn("chunk3of4", calls)
		self.assertIn("chunk4of4", calls)

	def test_the_results_are_listed_and_recorded(self) -> None:
		result = self._run("run_coda_chunks.sh", "2")
		self.assertIn("just coda-post out_coda_0_f0-5_20260101_000001 out_coda_0_f5-10_20260101_000001", result.stdout)
		rows = (self.out_root / "coda_chunks.tsv").read_text(encoding="utf-8").splitlines()
		self.assertEqual(rows[0].split("\t")[:6], ["1", "2", "0", "5", "0", "5"])
		self.assertEqual(rows[1].split("\t")[:6], ["2", "2", "5", "10", "0", "5"])

	def test_an_existing_complete_bag_is_reused(self) -> None:
		bag = self.coda / "coda_0_f0-5_20250101_000000.bag"
		bag.mkdir()
		(bag / "metadata.yaml").write_text("", encoding="utf-8")
		self._run("run_coda_chunks.sh", "2")
		calls = self._calls()
		self.assertNotIn("bag 0 5 coda_0_f0-5", calls)
		self.assertIn(f"run bag={bag} ", calls)
		self.assertIn("bag 5 5 coda_0_f5-10", calls)

	def test_a_bag_of_another_frame_range_is_not_reused(self) -> None:
		# 2 chunks of 5 frames: a complete bag of frames 0-4 (another split) must not stand in for frames 0-5
		bag = self.coda / "coda_0_f0-4_20250101_000000.bag"
		bag.mkdir()
		(bag / "metadata.yaml").write_text("", encoding="utf-8")
		self._run("run_coda_chunks.sh", "2")
		self.assertIn("bag 0 5 coda_0_f0-5\n", self._calls())

	def test_a_failed_chunk_does_not_stop_the_others_but_fails_the_command(self) -> None:
		result = self._run("run_coda_chunks.sh", "2", FAKE_RUN_RC="1", FAKE_NO_BAG="coda_0_f5-10")
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertIn("chunk 1/2: exit code 1", result.stdout)
		self.assertIn("chunk 2/2: no complete bag", result.stderr)
		self.assertIn("2 chunk(s) failed", result.stderr)

	def test_too_few_depth_maps_or_a_bad_count_stop_at_once(self) -> None:
		for args in (("11",), ("0",), ("x",)):
			result = self._run("run_coda_chunks.sh", *args)
			self.assertEqual(result.returncode, 2, (args, result.stdout + result.stderr))
		self.assertEqual(self._calls(), "")


class PostprocessCodaTest(CodaScriptTest):
	def setUp(self) -> None:
		super().setUp()
		self.results = self.root / "data" / "output" / "coda"
		self.results.mkdir(parents=True)
		for name, mtime in (("out_20260101_000001", 1000), ("out_20260101_000002", 2000)):
			run = self.results / name
			run.mkdir()
			(run / "dsg.json").write_text("{}", encoding="utf-8")
			(run / "performance_statistics.csv").write_text("operation,count\nprocess_frame,42\n", encoding="utf-8")
			os.utime(run, (mtime, mtime))
		# python scripts/<step>.py --data-dir DIR: records the call and writes the file the step produces
		self._write_executable(
			self.root / "fakebin" / "python",
			"""
			#!/usr/bin/env bash
			echo "python $* HF_HOME=$HF_HOME" >> "$CALLS"
			dir=""; prev=""
			for a in "$@"; do if [ "$prev" = --data-dir ]; then dir="$a"; fi; prev="$a"; done
			case "$1" in
			  scripts/postprocess_scene_graph.py) [ "${FAKE_FAIL_STEP:-}" = embeddings ] && exit 1; : > "$dir/dsg_updated.json" ;;
			  scripts/adapt_cluster_places.py)
			    dst=""; prev=""
			    for a in "$@"; do if [ "$prev" = --dst ]; then dst="$a"; fi; prev="$a"; done
			    : > "$dst"
			    ;;
			  scripts/summarize_regions.py) : > "$dir/clustered_dsg_with_summaries.json" ;;
			esac
			""",
		)
		self._write_executable(
			self.root / "fakebin" / "ros2",
			"""
			#!/usr/bin/env bash
			echo "ros2 $*" >> "$CALLS"
			for a in "$@"; do case "$a" in data_dir:=*) dir="${a#data_dir:=}";; save_dsg:=*) dst="${a#save_dsg:=}";; esac; done
			: > "${dst:-$dir/clustered_dsg.json}"
			""",
		)

	def _steps(self) -> list:
		return [line.split(" ")[1] for line in self._calls().splitlines()]

	def test_without_arguments_the_newest_result_is_processed(self) -> None:
		result = self._run("postprocess_coda.sh")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		calls = self._calls()
		self.assertIn(f"--data-dir {self.results / 'out_20260101_000002'}", calls)
		self.assertNotIn("out_20260101_000001", calls)
		self.assertEqual(self._steps(), ["scripts/postprocess_scene_graph.py", "launch"])

	def test_result_root_can_select_tum_outputs_without_a_coda_root(self) -> None:
		tum_results = self.root / "output" / "tum_replay_fr1_room"
		outer = tum_results / "out_20260102_000001"
		run = outer / "out_20260102_000005"
		run.mkdir(parents=True)
		(run / "dsg.json").write_text("{}", encoding="utf-8")
		result = self._run(
			"postprocess_coda.sh",
			RESULT_ROOT=str(tum_results),
			CODA_ROOT_DIR="",
			CLUSTER_PLACES_CONFIG="config/cluster_places_config_tum.yaml",
			PLACES_LAYER="PLACES",
		)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertIn(f"--data-dir {run}", self._calls())
		self.assertIn(
			"cluster_places_config_path:=config/cluster_places_config_tum.yaml",
			self._calls(),
		)
		self.assertIn("scripts/adapt_cluster_places.py prepare", self._calls())
		self.assertIn("scripts/adapt_cluster_places.py restore", self._calls())
		self.assertTrue((run / "clustered_dsg.json").exists())

		self.calls.unlink()
		result = self._run(
			"postprocess_coda.sh",
			outer.name,
			RESULT_ROOT=str(tum_results),
			CODA_ROOT_DIR="",
			CLUSTER_PLACES_CONFIG="config/cluster_places_config_tum.yaml",
			PLACES_LAYER="PLACES",
		)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertIn(f"--data-dir {run}", self._calls())

	def test_the_steps_run_in_order_and_the_sentence_model_cache_is_shared(self) -> None:
		self._run("postprocess_coda.sh", "out_20260101_000001")
		lines = self._calls().splitlines()
		self.assertIn("scripts/postprocess_scene_graph.py --data-dir " + str(self.results / "out_20260101_000001"), lines[0])
		self.assertTrue(lines[0].endswith("HF_HOME=/data/hf_cache"), lines[0])
		self.assertIn("cluster_places.launch.yaml data_dir:=" + str(self.results / "out_20260101_000001") + " interactive:=false", lines[1])
		self.assertNotIn("cluster_places_config_path:=", lines[1])
		self.assertEqual(len(lines), 2)

	def test_summaries_need_the_flag_and_a_key(self) -> None:
		without_key = self._run("postprocess_coda.sh", "--summaries", OPENAI_API_KEY="")
		self.assertEqual(without_key.returncode, 2, without_key.stdout + without_key.stderr)
		self.assertIn("OPENAI_API_KEY", without_key.stderr)
		self.assertEqual(self._calls(), "")
		result = self._run("postprocess_coda.sh", "--summaries", "out_20260101_000001", OPENAI_API_KEY="sk-test")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertIn("scripts/summarize_regions.py --data-dir " + str(self.results / "out_20260101_000001") + " --model-name gpt-5-nano --n-samples 20", self._calls())
		self.assertIn("--places-layer TRAVERSABILITY", self._calls())
		self.assertTrue((self.results / "out_20260101_000001" / "clustered_dsg_with_summaries.json").exists())

	def test_several_results_and_a_failing_step(self) -> None:
		result = self._run("postprocess_coda.sh", "out_20260101_000001", "out_20260101_000002", "out_missing", FAKE_FAIL_STEP="embeddings")
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertIn("out_missing: no dsg.json", result.stderr)
		self.assertIn("out_20260101_000001: the embedding step failed", result.stderr)
		self.assertIn("3 result(s) failed", result.stderr)
		self.assertNotIn("cluster_places", self._calls())

	def test_list_shows_the_results_newest_first_with_their_progress(self) -> None:
		older = self.results / "out_20260101_000001"
		(older / "dsg_updated.json").write_text("{}", encoding="utf-8")
		os.utime(older, (1000, 1000))   # adding a file touched the directory: it must stay the older result
		result = self._run("postprocess_coda.sh", "--list")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		rows = [line.split() for line in result.stdout.splitlines()[1:]]
		self.assertEqual([row[0] for row in rows], ["out_20260101_000002", "out_20260101_000001"])
		self.assertEqual(rows[0][1:3], ["42", "frames"])
		self.assertIn("embeddings yes", " ".join(rows[1]))
		self.assertIn("embeddings no", " ".join(rows[0]))
		self.assertEqual(self._calls(), "")


class RunCodaExampleBagNameTest(CodaScriptTest):
	def test_a_bag_name_without_a_directory_is_looked_up_under_the_data_root(self) -> None:
		(self.coda / "coda_0_chunk1of4_20260101_000001.bag").mkdir()
		result = self._run("run_coda_example.sh", "coda_0_chunk1of4_20260101_000001.bag", READY_TIMEOUT="2")
		# no ros2 on the fake PATH that would launch anything: the script must get past the bag lookup and fail later
		self.assertNotIn("bag not found", result.stderr)

	def test_a_missing_bag_is_reported(self) -> None:
		result = self._run("run_coda_example.sh", "nothing.bag")
		self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
		self.assertIn("bag not found", result.stderr)

	def _bag_with_frames(self, rgb_frames: int, name: str = "long.bag") -> Path:
		bag = self.coda / name
		bag.mkdir()
		(bag / "metadata.yaml").write_text(
			textwrap.dedent(
				f"""\
				rosbag2_bagfile_information:
				  topics_with_message_count:
				  - topic_metadata:
				      name: /cam0/camera_info
				      type: sensor_msgs/msg/CameraInfo
				    message_count: 111
				  - topic_metadata:
				      name: /cam0/rgb_image
				      type: sensor_msgs/msg/Image
				      serialization_format: cdr
				    message_count: {rgb_frames}
				"""
			),
			encoding="utf-8",
		)
		return bag

	def test_a_bag_too_long_for_the_memory_gets_a_warning(self) -> None:
		bag = self._bag_with_frames(8213)
		result = self._run("run_coda_example.sh", str(bag))
		self.assertIn("WARNING", result.stderr)
		self.assertIn("has 8213 frames", result.stderr)
		self.assertIn("just coda-chunks", result.stderr)

	def test_a_short_bag_or_a_raised_limit_gets_no_warning(self) -> None:
		short = self._bag_with_frames(2054, "short.bag")
		self.assertNotIn("WARNING", self._run("run_coda_example.sh", str(short)).stderr)
		# a lower limit makes the same bag too long, a higher one silences a long bag
		self.assertIn("WARNING", self._run("run_coda_example.sh", str(short), LONG_BAG_FRAMES="1000").stderr)
		long_bag = self._bag_with_frames(8213, "long.bag")
		self.assertNotIn("WARNING", self._run("run_coda_example.sh", str(long_bag), LONG_BAG_FRAMES="10000").stderr)


if __name__ == "__main__":
	unittest.main()
