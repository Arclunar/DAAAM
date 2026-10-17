import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = ("make_coda_bag.sh", "run_coda_chunks.sh", "postprocess_coda.sh", "run_coda_example.sh")
CODA_ORIGIN = "1673884185.589118"
SIDECAR = {"dataset_kind": "coda", "sequence": "0", "start_frame": 821, "frame_count": 821, "fps": 10.0, "time_origin": 1673884185.589118}


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
			  mkdir -p "$dir"
			  [ "${FAKE_NO_METADATA:-0}" = 1 ] || : > "$dir/metadata.yaml"
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
		(self.coda / "timestamps").mkdir()
		stamps = [float(CODA_ORIGIN) + 0.1 * index for index in range(8213)]
		(self.coda / "timestamps" / "0.txt").write_text(CODA_ORIGIN + "\n" + "".join("%.6f\n" % t for t in stamps[1:]), encoding="utf-8")

	def _sidecar(self, bag: str) -> dict:
		return json.loads((self.coda / bag / "daaam_replay.json").read_text(encoding="utf-8"))

	def test_a_complete_bag_gets_the_replay_sidecar(self) -> None:
		result = self._run("make_coda_bag.sh", "0", "1000")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		bag = "coda_0_with_depth_20260101_000001.bag"
		self.assertEqual(
			self._sidecar(bag),
			{"dataset_kind": "coda", "sequence": "0", "start_frame": 0, "frame_count": 1000, "fps": 10.0, "time_origin": 1673884185.589118},
		)
		self.assertEqual(sorted(p.name for p in (self.coda / bag).iterdir()), ["daaam_replay.json", "metadata.yaml"], "no temporary file is left")
		self.assertIn("daaam_replay.json", result.stdout)

	def test_a_bag_to_the_end_of_the_sequence_records_the_remaining_frames(self) -> None:
		result = self._run("make_coda_bag.sh", "500", "0", "tail")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		sidecar = self._sidecar("tail_20260101_000001.bag")
		self.assertEqual((sidecar["start_frame"], sidecar["frame_count"]), (500, 7713))

	def test_an_unclosed_bag_gets_no_sidecar(self) -> None:
		result = self._run("make_coda_bag.sh", "0", "10", FAKE_NO_METADATA="1")
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertEqual(list(self.coda.rglob("daaam_replay.json")), [])

	def test_a_range_beyond_the_sequence_is_rejected_before_recording(self) -> None:
		for args in (("8213", "1"), ("8000", "500"), ("9000", "0")):
			result = self._run("make_coda_bag.sh", *args)
			self.assertEqual(result.returncode, 2, (args, result.stdout + result.stderr))
			self.assertIn("8213 frames", result.stderr)
		self.assertEqual(self._calls(), "")

	def test_without_the_sequence_timestamps_no_bag_is_recorded(self) -> None:
		(self.coda / "timestamps" / "0.txt").unlink()
		result = self._run("make_coda_bag.sh", "0", "10")
		self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
		self.assertIn("timestamps/0.txt", result.stderr)
		self.assertEqual(self._calls(), "")

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
		self.assertFalse((old / "daaam_replay.json").exists(), "an older bag of the same name gets no sidecar")


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
			if [ "${FAKE_NO_BAG:-}" != "$3" ]; then mkdir -p "$dir" && : > "$dir/metadata.yaml" && echo '{}' > "$dir/daaam_replay.json"; fi
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
		(bag / "daaam_replay.json").write_text("{}", encoding="utf-8")
		self._run("run_coda_chunks.sh", "2")
		calls = self._calls()
		self.assertNotIn("bag 0 5 coda_0_f0-5", calls)
		self.assertIn(f"run bag={bag} ", calls)
		self.assertIn("bag 5 5 coda_0_f5-10", calls)

	def test_a_bag_recorded_before_the_replay_sidecar_is_recorded_again(self) -> None:
		old = self.coda / "coda_0_f0-5_20250101_000000.bag"
		old.mkdir()
		(old / "metadata.yaml").write_text("", encoding="utf-8")
		self._run("run_coda_chunks.sh", "2")
		calls = self._calls()
		self.assertIn("bag 0 5 coda_0_f0-5\n", calls)
		self.assertNotIn(f"run bag={old} ", calls)

	def test_a_bag_of_another_frame_range_is_not_reused(self) -> None:
		# 2 chunks of 5 frames: a complete bag of frames 0-4 (another split) must not stand in for frames 0-5
		bag = self.coda / "coda_0_f0-4_20250101_000000.bag"
		bag.mkdir()
		(bag / "metadata.yaml").write_text("", encoding="utf-8")
		(bag / "daaam_replay.json").write_text("{}", encoding="utf-8")
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
		# python scripts/<step>.py --data-dir DIR [--output F | --summary-output F --scene-graph-output F]: records the call and
		# writes the files the step produces (to the explicit paths when given)
		self._write_executable(
			self.root / "fakebin" / "python",
			"""
			#!/usr/bin/env bash
			echo "python $* HF_HOME=$HF_HOME" >> "$CALLS"
			dir=""; out=""; summary_out=""; graph_out=""; prev=""
			for a in "$@"; do
			  case "$prev" in
			    --data-dir) dir="$a" ;;
			    --output) out="$a" ;;
			    --summary-output) summary_out="$a" ;;
			    --scene-graph-output) graph_out="$a" ;;
			  esac
			  prev="$a"
			done
			case "$1" in
			  scripts/postprocess_scene_graph.py) [ "${FAKE_FAIL_STEP:-}" = embeddings ] && exit 1; echo '{"nodes": [], "fake": "embeddings"}' > "${out:-$dir/dsg_updated.json}" ;;
			  scripts/adapt_cluster_places.py)
			    dst=""; prev=""
			    for a in "$@"; do if [ "$prev" = --dst ]; then dst="$a"; fi; prev="$a"; done
			    echo '{"nodes": [], "fake": "adapted"}' > "$dst"
			    ;;
			  scripts/summarize_regions.py)
			    [ "${FAKE_FAIL_STEP:-}" = summaries ] && exit 1
			    echo '{"nodes": [], "fake": "summarized"}' > "${graph_out:-$dir/clustered_dsg_with_summaries.json}"
			    echo 'region_summaries: []' > "${summary_out:-$dir/region_summaries.yaml}"
			    ;;
			esac
			""",
		)
		self._write_executable(
			self.root / "fakebin" / "ros2",
			"""
			#!/usr/bin/env bash
			echo "ros2 $*" >> "$CALLS"
			for a in "$@"; do case "$a" in data_dir:=*) dir="${a#data_dir:=}";; save_dsg:=*) dst="${a#save_dsg:=}";; esac; done
			[ "${FAKE_FAIL_STEP:-}" = clustering ] && exit 1
			echo '{"nodes": [], "fake": "clustered"}' > "${dst:-$dir/clustered_dsg.json}"
			if [ -n "${FAKE_HANG_FILE:-}" ]; then : > "$FAKE_HANG_FILE"; exec sleep 31.317; fi
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

	# ------------------------------------------------------------------ stages
	def _clustered(self, name: str = "out_20260101_000001") -> Path:
		run = self.results / name
		(run / "clustered_dsg.json").write_text("CLUSTERED", encoding="utf-8")
		return run

	def test_stage_summarize_runs_only_the_summaries_of_a_clustered_result(self) -> None:
		run = self._clustered()
		result = self._run("postprocess_coda.sh", "--stage", "summarize", "out_20260101_000001", OPENAI_API_KEY="sk-test")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertEqual(self._steps(), ["scripts/summarize_regions.py"])
		self.assertNotIn("postprocess_scene_graph.py", self._calls())
		self.assertNotIn("launch", self._calls())
		self.assertTrue((run / "clustered_dsg_with_summaries.json").exists())
		self.assertTrue((run / "region_summaries.yaml").exists())
		self.assertEqual((run / "clustered_dsg.json").read_text(encoding="utf-8"), "CLUSTERED")

	def test_stage_postprocess_never_summarizes_even_with_a_key(self) -> None:
		for arguments in (("--stage", "postprocess"), ("--stage=postprocess",), ()):
			with self.subTest(arguments=arguments):
				if self.calls.exists():
					self.calls.unlink()
				result = self._run("postprocess_coda.sh", *arguments, "out_20260101_000001", OPENAI_API_KEY="sk-test")
				self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
				self.assertEqual(self._steps(), ["scripts/postprocess_scene_graph.py", "launch"])
				self.assertNotIn("summarize_regions", self._calls())

	def test_stage_all_and_the_summaries_flag_run_the_same_steps(self) -> None:
		runs = []
		for arguments in (("--stage", "all"), ("--summaries",), ("--stage", "all", "--summaries"), ("--summaries", "--stage=all")):
			if self.calls.exists():
				self.calls.unlink()
			result = self._run("postprocess_coda.sh", *arguments, "out_20260101_000001", OPENAI_API_KEY="sk-test")
			self.assertEqual(result.returncode, 0, "%s: %s%s" % (arguments, result.stdout, result.stderr))
			runs.append(self._steps())
		self.assertEqual(runs[0], ["scripts/postprocess_scene_graph.py", "launch", "scripts/summarize_regions.py"])
		self.assertEqual(runs, [runs[0]] * 4)

	def test_summarize_refuses_a_result_without_the_clustering(self) -> None:
		result = self._run("postprocess_coda.sh", "--stage", "summarize", "out_20260101_000001", OPENAI_API_KEY="sk-test")
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertIn("clustered_dsg.json", result.stderr)
		self.assertIn("out_20260101_000001", result.stderr)
		self.assertEqual(self._calls(), "")

	def test_unknown_stages_and_conflicting_flags_exit_2_before_anything_runs(self) -> None:
		for arguments in (("--stage", "bogus"), ("--stage",), ("--stage", ""), ("--stage=", ), ("--stage", "postprocess", "--summaries"), ("--stage", "summarize", "--summaries")):
			with self.subTest(arguments=arguments):
				result = self._run("postprocess_coda.sh", *arguments, OPENAI_API_KEY="sk-test")
				self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
				self.assertEqual(self._calls(), "")
		self.assertIn("postprocess, summarize or all", self._run("postprocess_coda.sh", "--stage", "bogus").stderr)

	def test_the_summary_stages_need_the_key_but_postprocess_does_not(self) -> None:
		self._clustered()
		for stage in ("summarize", "all"):
			with self.subTest(stage=stage):
				result = self._run("postprocess_coda.sh", "--stage", stage, "out_20260101_000001", OPENAI_API_KEY="")
				self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
				self.assertIn("OPENAI_API_KEY", result.stderr)
				self.assertEqual(self._calls(), "")
		result = self._run("postprocess_coda.sh", "--stage", "postprocess", "out_20260101_000001", OPENAI_API_KEY="")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

	def test_a_failing_summary_step_keeps_the_postprocess_outputs_of_a_full_run(self) -> None:
		result = self._run("postprocess_coda.sh", "--atomic", "--stage", "all", "out_20260101_000001", OPENAI_API_KEY="sk-test", FAKE_FAIL_STEP="summaries")
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertIn("the summary step failed", result.stderr)
		run = self.results / "out_20260101_000001"
		self.assertTrue((run / "dsg_updated.json").exists() and (run / "clustered_dsg.json").exists(), "the finished stage stays published")
		self.assertFalse((run / "clustered_dsg_with_summaries.json").exists())

	# ------------------------------------------------------------------ atomic outputs
	OLD = {
		"dsg_updated.json": "OLD-UPDATED",
		"clustered_dsg.json": "OLD-CLUSTERED",
		"clustered_dsg_with_summaries.json": "OLD-SUMMARY-GRAPH",
		"region_summaries.yaml": "OLD-YAML",
	}

	def _old_outputs(self, name: str = "out_20260101_000001") -> Path:
		run = self.results / name
		for file, text in self.OLD.items():
			(run / file).write_text(text, encoding="utf-8")
		return run

	def _contents(self, run: Path) -> dict:
		return {file: (run / file).read_text(encoding="utf-8") for file in self.OLD if (run / file).exists()}

	def _leftovers(self, run: Path) -> list:
		return sorted(path.name for path in run.iterdir() if path.name.startswith(".replay-job-") or path.name.startswith(".cluster_places_"))

	def test_atomic_postprocess_publishes_both_graphs_and_leaves_no_temporaries(self) -> None:
		run = self.results / "out_20260101_000001"
		result = self._run("postprocess_coda.sh", "--atomic", "--stage", "postprocess", "out_20260101_000001")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		calls = self._calls().splitlines()
		match = re.search(r"--data-dir %s --output (%s/\.replay-job-(\d+)-dsg_updated\.json) HF_HOME" % (re.escape(str(run)), re.escape(str(run))), calls[0])
		self.assertIsNotNone(match, calls[0])
		updated_tmp, pid = match.group(1), match.group(2)
		clustered_tmp = "%s/.replay-job-%s-clustered_dsg.json" % (run, pid)
		self.assertIn("data_dir:=%s interactive:=false src_dsg:=%s save_dsg:=%s" % (run, updated_tmp, clustered_tmp), calls[1])
		self.assertEqual(len(calls), 2)
		self.assertIn('"fake": "embeddings"', (run / "dsg_updated.json").read_text(encoding="utf-8"))
		self.assertIn('"fake": "clustered"', (run / "clustered_dsg.json").read_text(encoding="utf-8"))
		self.assertEqual(self._leftovers(run), [])
		self.assertIn("done (dsg_updated.json clustered_dsg.json)", result.stdout)

	def test_atomic_clustering_of_a_places_graph_goes_through_the_same_temporaries(self) -> None:
		run = self.results / "out_20260101_000001"
		result = self._run("postprocess_coda.sh", "--atomic", "out_20260101_000001", PLACES_LAYER="PLACES", CLUSTER_PLACES_CONFIG="config/cluster_places_config_tum.yaml")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		calls = self._calls()
		pid = re.search(r"\.replay-job-(\d+)-dsg_updated\.json", calls).group(1)
		updated_tmp, clustered_tmp = "%s/.replay-job-%s-dsg_updated.json" % (run, pid), "%s/.replay-job-%s-clustered_dsg.json" % (run, pid)
		self.assertIn("adapt_cluster_places.py prepare --src %s --dst %s/.cluster_places_input.json --layer PLACES" % (updated_tmp, run), calls)
		self.assertIn("src_dsg:=%s/.cluster_places_input.json save_dsg:=%s/.cluster_places_output.json" % (run, run), calls)
		self.assertIn("adapt_cluster_places.py restore --src %s/.cluster_places_output.json --original %s --dst %s --layer PLACES" % (run, updated_tmp, clustered_tmp), calls)
		self.assertTrue((run / "clustered_dsg.json").exists())
		self.assertEqual(self._leftovers(run), [])

	def test_a_failing_atomic_step_leaves_every_existing_output_byte_identical(self) -> None:
		for step, arguments in (("embeddings", ("--stage", "postprocess")), ("clustering", ("--stage", "postprocess")), ("summaries", ("--stage", "summarize"))):
			with self.subTest(step=step):
				run = self._old_outputs()
				before = self._contents(run)
				result = self._run("postprocess_coda.sh", "--atomic", *arguments, "out_20260101_000001", OPENAI_API_KEY="sk-test", FAKE_FAIL_STEP=step)
				self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
				self.assertEqual(self._contents(run), before)
				self.assertEqual(self._leftovers(run), [])

	def test_a_failing_non_atomic_embedding_step_keeps_the_previous_behaviour(self) -> None:
		result = self._run("postprocess_coda.sh", "out_20260101_000001", FAKE_FAIL_STEP="embeddings")
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertNotIn("--output", self._calls(), "without --atomic the steps are called exactly as before")

	def test_atomic_summaries_publish_the_graph_and_the_yaml_together(self) -> None:
		run = self._old_outputs()
		result = self._run("postprocess_coda.sh", "--atomic", "--stage", "summarize", "out_20260101_000001", OPENAI_API_KEY="sk-test")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertRegex(self._calls(), r"summarize_regions\.py --data-dir %s --model-name gpt-5-nano --n-samples 20 --places-layer TRAVERSABILITY --summary-output %s/\.replay-job-\d+-region_summaries\.yaml --scene-graph-output %s/\.replay-job-\d+-clustered_dsg_with_summaries\.json" % ((re.escape(str(run)),) * 3))
		contents = self._contents(run)
		self.assertIn("summarized", contents["clustered_dsg_with_summaries.json"])
		self.assertEqual(contents["region_summaries.yaml"], "region_summaries: []\n")
		self.assertEqual(contents["clustered_dsg.json"], "OLD-CLUSTERED", "the stage touches only its own outputs")
		self.assertEqual(contents["dsg_updated.json"], "OLD-UPDATED")
		self.assertEqual(self._leftovers(run), [])

	def test_a_publish_that_fails_halfway_puts_the_previous_files_back(self) -> None:
		self._write_executable(
			self.root / "fakebin" / "mv",
			"""
			#!/usr/bin/env bash
			case "$*" in
			  *".replay-job-"*"-region_summaries.yaml"*) echo "mv: simulated failure" >&2; exit 1 ;;
			esac
			exec /bin/mv "$@"
			""",
		)
		run = self._old_outputs()
		before = self._contents(run)
		result = self._run("postprocess_coda.sh", "--atomic", "--stage", "summarize", "out_20260101_000001", OPENAI_API_KEY="sk-test")
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertEqual(self._contents(run), before, "the graph that was already replaced is restored")
		self.assertEqual(self._leftovers(run), [])
		self.assertIn("the summary step failed", result.stderr)
		# a first publish that fails halfway leaves nothing behind either
		for file in ("clustered_dsg_with_summaries.json", "region_summaries.yaml"):
			(run / file).unlink()
		result = self._run("postprocess_coda.sh", "--atomic", "--stage", "summarize", "out_20260101_000001", OPENAI_API_KEY="sk-test")
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertFalse((run / "clustered_dsg_with_summaries.json").exists())
		self.assertFalse((run / "region_summaries.yaml").exists())
		self.assertEqual(self._leftovers(run), [])

	def test_a_terminated_run_removes_only_its_own_temporaries(self) -> None:
		run = self._old_outputs()
		before = self._contents(run)
		foreign = run / ".replay-job-1-dsg_updated.json"
		foreign.write_text("another process", encoding="utf-8")
		started = self.root / "started"
		process = subprocess.Popen(
			["bash", "scripts/postprocess_coda.sh", "--atomic", "--stage", "postprocess", "out_20260101_000001"],
			cwd=self.root, env=self._env(FAKE_HANG_FILE=str(started)), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
			text=True, start_new_session=True,
		)
		try:
			deadline = time.monotonic() + 20
			while not started.exists():
				self.assertLess(time.monotonic(), deadline, "the fake clustering never started")
				time.sleep(0.02)
			own = sorted(path.name for path in run.iterdir() if path.name.startswith(".replay-job-%d-" % process.pid))
			self.assertEqual(own, [".replay-job-%d-clustered_dsg.json" % process.pid, ".replay-job-%d-dsg_updated.json" % process.pid])
			os.killpg(process.pid, signal.SIGTERM)
			process.communicate(timeout=20)
		finally:
			if process.poll() is None:
				os.killpg(process.pid, signal.SIGKILL)
				process.communicate()
		self.assertEqual(process.returncode, 143)
		self.assertEqual(self._leftovers(run), [foreign.name])
		self.assertEqual(self._contents(run), before)

	@staticmethod
	def _running_fake_steps() -> list:
		"""Processes whose command is exactly the fake clustering's endless sleep (found without matching this very search)."""
		found = []
		for entry in Path("/proc").iterdir():
			if entry.name.isdigit():
				try:
					if (entry / "cmdline").read_bytes().split(b"\0")[:2] == [b"sleep", b"31.317"]:
						found.append(entry.name)
				except OSError:
					pass
		return found

	def test_a_stop_signal_to_the_script_alone_stops_its_step_at_once_and_cleans_up(self) -> None:
		"""`docker stop` signals the container's first process only: the script must not wait for its step to finish."""
		run = self._old_outputs()
		before = self._contents(run)
		started = self.root / "started"
		process = subprocess.Popen(
			["bash", "scripts/postprocess_coda.sh", "--atomic", "--stage", "postprocess", "out_20260101_000001"],
			cwd=self.root, env=self._env(FAKE_HANG_FILE=str(started)), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
			text=True, start_new_session=True,
		)
		try:
			deadline = time.monotonic() + 20
			while not started.exists():
				self.assertLess(time.monotonic(), deadline, "the fake clustering never started")
				time.sleep(0.02)
			moment = time.monotonic()
			os.kill(process.pid, signal.SIGTERM)   # the script only, not its group
			process.communicate(timeout=10)
			elapsed = time.monotonic() - moment
		finally:
			if process.poll() is None:
				os.killpg(process.pid, signal.SIGKILL)
				process.communicate()
		self.assertEqual(process.returncode, 143)
		self.assertLess(elapsed, 5, "the script waited for its step instead of stopping it")
		self.assertEqual(self._leftovers(run), [])
		self.assertEqual(self._contents(run), before)
		self.assertEqual(self._running_fake_steps(), [], "the step was left running")

	def test_the_python_steps_accept_explicit_output_paths(self) -> None:
		embeddings = (REPO / "scripts" / "postprocess_scene_graph.py").read_text(encoding="utf-8")
		summaries = (REPO / "scripts" / "summarize_regions.py").read_text(encoding="utf-8")
		self.assertTrue("'--output'" in embeddings, "postprocess_scene_graph.py has no --output")
		self.assertTrue("'--summary-output'" in summaries, "summarize_regions.py has no --summary-output")
		self.assertTrue("'--scene-graph-output'" in summaries, "summarize_regions.py has no --scene-graph-output")
		self.assertTrue("os.replace(" in summaries, "the YAML is published by rename in every mode")

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


class RunCodaExampleReplayTest(CodaScriptTest):
	"""After the run, the result created by this invocation gets a one-snapshot replay bundle from its final graph."""

	NEW = "output/coda/out_20261017_000001"

	def setUp(self) -> None:
		super().setUp()
		self.flag = self.root / "bag-played"
		self._write_executable(self.root / "fakebin" / "sleep", "#!/usr/bin/env bash\nexit 0\n")
		self._write_executable(
			self.root / "fakebin" / "ros2",
			"""
			#!/usr/bin/env bash
			echo "ros2 $*" >> "$CALLS"
			case "$1 $2" in
			"launch daaam_ros")
				echo "ROS components initialized"
				/bin/sleep 0.1   # the run takes time: its result is newer than the marker taken before the launch
				if [ "${FAKE_NO_RESULT:-0}" != 1 ]; then mkdir -p output/coda/out_20261017_000001 && echo '{}' > output/coda/out_20261017_000001/dsg.json; fi
				for i in $(seq 1 200); do [ -f "$FLAG" ] && break; /bin/sleep 0.05; done
				exit "${FAKE_LAUNCH_RC:-0}"
				;;
			"service call") : > "$FLAG" ;;
			esac
			exit 0
			""",
		)
		self._write_executable(
			self.root / "fakebin" / "python",
			"""
			#!/usr/bin/env bash
			echo "python $*" >> "$CALLS"
			if [ "$1" = scripts/replay.py ] && [ "$2" = finalize ]; then
			out=""; prev=""
			for a in "$@"; do if [ "$prev" = --out ]; then out="$a"; fi; prev="$a"; done
			[ "${FAKE_FINALIZE_RC:-0}" = 0 ] || exit "${FAKE_FINALIZE_RC}"
			mkdir -p "$out" && : > "$out/manifest.json"
			fi
			exit 0
			""",
		)
		self.older = self.root / "data" / "output" / "coda" / "out_20261016_000000"
		self.older.mkdir(parents=True)
		(self.older / "dsg.json").write_text("{}", encoding="utf-8")
		os.utime(self.older, (1000, 1000))
		# the name says frames 5-9: the sidecar, not the name, says what the bag holds
		self.bag = self.coda / "coda_0_f5-9_20260101_000001.bag"
		self.bag.mkdir()
		(self.bag / "metadata.yaml").write_text("", encoding="utf-8")

	def _sidecar(self, value=None) -> None:
		(self.bag / "daaam_replay.json").write_text(json.dumps(SIDECAR if value is None else value), encoding="utf-8")

	def _run_example(self, **env: str) -> subprocess.CompletedProcess:
		return self._run("run_coda_example.sh", str(self.bag), FLAG=str(self.flag), **env)

	def _finalize_calls(self) -> list:
		return [line for line in self._calls().splitlines() if "scripts/replay.py finalize" in line]

	def test_the_new_result_is_finalized_with_the_sidecar_metadata(self) -> None:
		self._sidecar()
		result = self._run_example()
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		calls = self._finalize_calls()
		self.assertEqual(len(calls), 1, self._calls())
		call = calls[0]
		self.assertIn(f"--graph {self.NEW}/dsg.json --run {self.NEW}", call)
		self.assertIn("--dataset-kind coda --sequence 0", call)
		self.assertIn(f"--dataset {self.coda}", call)
		self.assertIn("--fps 10.0 --time-origin 1673884185.589118", call)
		self.assertIn("--start-frame 821 --frame-count 821", call)
		self.assertIn(f"--out {self.NEW}/replay", call)
		self.assertTrue((self.root / self.NEW / "replay" / "manifest.json").is_file())
		self.assertFalse((self.older / "replay").exists(), "an older result never gets the bundle")
		self.assertIn(f"Results: {self.NEW}", result.stdout)
		self.assertIn("just replay-web", result.stdout)

	def test_a_bag_without_a_sidecar_keeps_the_result_and_fails_the_artifact(self) -> None:
		result = self._run_example()
		self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertEqual(self._finalize_calls(), [])
		self.assertTrue((self.root / self.NEW / "dsg.json").is_file())
		self.assertFalse((self.root / self.NEW / "replay").exists())
		self.assertIn("daaam_replay.json", result.stderr)
		self.assertIn("just coda-bag", result.stderr)
		self.assertIn(f"Results: {self.NEW}", result.stdout, "the mapping result is still reported")

	def test_a_malformed_sidecar_is_not_used(self) -> None:
		self._sidecar({**SIDECAR, "dataset_kind": "tum"})
		result = self._run_example()
		self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertEqual(self._finalize_calls(), [])
		self.assertIn("daaam_replay.json", result.stderr)

	def test_without_a_new_result_nothing_is_attached_to_an_older_one(self) -> None:
		self._sidecar()
		result = self._run_example(FAKE_NO_RESULT="1")
		self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertEqual(self._finalize_calls(), [])
		self.assertFalse((self.older / "replay").exists())
		self.assertNotIn("Results:", result.stdout)

	def test_a_failed_pipeline_keeps_its_exit_code(self) -> None:
		self._sidecar()
		result = self._run_example(FAKE_LAUNCH_RC="4")
		self.assertEqual(result.returncode, 4, result.stdout + result.stderr)

	def test_a_failed_finalize_fails_the_artifact_but_keeps_the_result(self) -> None:
		self._sidecar()
		result = self._run_example(FAKE_FINALIZE_RC="1")
		self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertTrue((self.root / self.NEW / "dsg.json").is_file())
		self.assertIn(f"Results: {self.NEW}", result.stdout)


if __name__ == "__main__":
	unittest.main()
