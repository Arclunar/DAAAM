from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
FAKE_CACHE = "/fake/cache/open3d_tsdf_1cm.npz"


class RunReplayTest(unittest.TestCase):
	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.root = Path(self.temp.name)
		(self.root / "scripts").mkdir()
		(self.root / "fakebin").mkdir()
		shutil.copy(REPO / "scripts" / "run_replay.sh", self.root / "scripts" / "run_replay.sh")
		self._write_executable(
			self.root / "scripts" / "run_tum_example.sh",
			"""
			#!/usr/bin/env bash
			[ -z "${FAKE_LOG:-}" ] || echo pipeline >> "${FAKE_LOG}"
			if [ "${FAKE_CREATE_RUN:-1}" = 1 ]; then
			  mkdir -p "output/${NAME}/out_new"
			fi
			exit "${FAKE_PIPELINE_RC:-0}"
			""",
		)
		self._write_executable(
			self.root / "fakebin" / "python",
			f"""
			#!/usr/bin/env bash
			[ -z "${{FAKE_LOG:-}}" ] || echo "$*" >> "${{FAKE_LOG}}"
			case "$1" in
			  *build_reference_mesh.py)
			    if [ "${{FAKE_REFERENCE_RC:-0}}" != 0 ]; then echo "fake builder failure" >&2; exit "${{FAKE_REFERENCE_RC}}"; fi
			    echo "{FAKE_CACHE}"
			    exit 0
			    ;;
			esac
			mode="$2"
			if [ "$mode" = record ]; then
			  trap 'exit "${{FAKE_RECORDER_RC:-0}}"' TERM
			  while true; do sleep 0.05; done
			fi
			out=""
			while [ "$#" -gt 0 ]; do
			  if [ "$1" = --out ]; then out="$2"; shift 2; else shift; fi
			done
			mkdir -p "$(dirname "$out")"
			: > "$out"
			""",
		)

	def tearDown(self) -> None:
		self.temp.cleanup()

	def _write_executable(self, path: Path, body: str) -> None:
		path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
		path.chmod(0o755)

	def _dataset(self) -> str:
		(self.root / "dataset" / "rgb").mkdir(parents=True, exist_ok=True)
		return str(self.root / "dataset")

	def _calls(self) -> list:
		log = self.root / "calls.log"
		return log.read_text().splitlines() if log.exists() else []

	def _run(self, **overrides: str) -> subprocess.CompletedProcess[str]:
		env = os.environ.copy()
		for name in ("REFERENCE_MESH", "REFERENCE_MESH_CACHE"):
			env.pop(name, None)
		env.update(
			{
				"PATH": f"{self.root / 'fakebin'}:{env['PATH']}",
				"NAME": "test_run",
				"DATASET": str(self.root / "missing-dataset"),
				"FAKE_LOG": str(self.root / "calls.log"),
				**overrides,
			}
		)
		return subprocess.run(
			["bash", "scripts/run_replay.sh", "1", "15"],
			cwd=self.root,
			env=env,
			capture_output=True,
			text=True,
			timeout=20,
		)

	def _call(self, marker: str) -> str:
		matches = [call for call in self._calls() if marker in call]
		self.assertEqual(len(matches), 1, f"expected one call containing {marker!r}, got {self._calls()}")
		return matches[0]

	def test_recorder_failure_is_propagated_and_raw_snapshots_are_kept(self) -> None:
		result = self._run(FAKE_RECORDER_RC="7")
		self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertTrue((self.root / "output/test_run/.replay-recording").is_dir())

	def test_pipeline_failure_keeps_raw_snapshots(self) -> None:
		result = self._run(FAKE_PIPELINE_RC="9")
		self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
		self.assertTrue((self.root / "output/test_run/.replay-recording").is_dir())

	def test_old_run_directory_is_not_reused(self) -> None:
		old = self.root / "output/test_run/out_old"
		old.mkdir(parents=True)
		result = self._run(FAKE_CREATE_RUN="0")
		self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertFalse((old / "replay.html").exists())
		self.assertFalse((old / "replay.rrd").exists())

	def test_without_a_dataset_there_is_no_reference_mesh_to_build(self) -> None:
		result = self._run()
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertEqual([call for call in self._calls() if "build_reference_mesh.py" in call], [])
		self.assertNotIn("--reference-mesh", self._call("replay.py build"))

	def test_auto_builds_the_reference_after_the_pipeline_and_embeds_it_in_the_page_only(self) -> None:
		dataset = self._dataset()
		result = self._run(DATASET=dataset)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		calls = self._calls()
		pipeline = calls.index("pipeline")
		builder = next(i for i, call in enumerate(calls) if "build_reference_mesh.py" in call)
		page = next(i for i, call in enumerate(calls) if "replay.py build" in call)
		self.assertLess(pipeline, builder, "the reconstruction must not slow the mapping run down")
		self.assertLess(builder, page)
		self.assertTrue(calls[builder].endswith(f"build_reference_mesh.py {dataset}"), calls[builder])
		self.assertIn(f"--reference-mesh {FAKE_CACHE}", calls[page])
		self.assertNotIn("--reference-mesh", self._call("replay.py rrd"))
		self.assertTrue(next(self.root.glob("output/test_run/out_*/replay.html")).is_file())

	def test_off_skips_the_reference_mesh_completely(self) -> None:
		result = self._run(DATASET=self._dataset(), REFERENCE_MESH="off")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertEqual([call for call in self._calls() if "build_reference_mesh.py" in call], [])
		self.assertNotIn("--reference-mesh", self._call("replay.py build"))

	def test_rebuild_forces_a_new_cache(self) -> None:
		result = self._run(DATASET=self._dataset(), REFERENCE_MESH="rebuild")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertIn(" --force", self._call("build_reference_mesh.py"))
		self.assertIn(f"--reference-mesh {FAKE_CACHE}", self._call("replay.py build"))

	def test_the_cache_directory_override_reaches_the_builder(self) -> None:
		result = self._run(DATASET=self._dataset(), REFERENCE_MESH_CACHE="/writable/cache")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		builder = self._call("build_reference_mesh.py")
		self.assertIn("--cache-dir /writable/cache", builder)
		self.assertNotIn("--force", builder)

	def test_an_automatic_failure_still_produces_the_dynamic_only_replay(self) -> None:
		result = self._run(DATASET=self._dataset(), FAKE_REFERENCE_RC="1")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertIn("dynamic-only", result.stderr)
		self.assertNotIn("--reference-mesh", self._call("replay.py build"))
		self.assertTrue(next(self.root.glob("output/test_run/out_*/replay.html")).is_file())
		self.assertTrue(next(self.root.glob("output/test_run/out_*/replay.rrd")).is_file())

	def test_a_failed_rebuild_fails_the_run_but_keeps_the_dynamic_page_and_the_raw_snapshots(self) -> None:
		result = self._run(DATASET=self._dataset(), REFERENCE_MESH="rebuild", FAKE_REFERENCE_RC="1")
		self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertIn("could not be built", result.stderr)
		self.assertNotIn("--reference-mesh", self._call("replay.py build"))
		self.assertTrue((self.root / "output/test_run/.replay-recording").is_dir())
		self.assertTrue(next(self.root.glob("output/test_run/out_*/replay.html")).is_file())

	def test_an_invalid_mode_fails_before_the_pipeline_starts(self) -> None:
		result = self._run(DATASET=self._dataset(), REFERENCE_MESH="maybe")
		self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
		self.assertIn("REFERENCE_MESH must be auto, off or rebuild", result.stderr)
		self.assertEqual(self._calls(), [], "nothing may have run")

	def test_no_run_directory_means_no_reference_reconstruction(self) -> None:
		result = self._run(DATASET=self._dataset(), FAKE_CREATE_RUN="0")
		self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertEqual([call for call in self._calls() if "build_reference_mesh.py" in call], [])


if __name__ == "__main__":
	unittest.main()
