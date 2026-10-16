import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np


REPO = Path(__file__).resolve().parents[1]


def write_snapshot(path: Path, frame: int, offset: float) -> None:
	arrays = {
		"mesh_pos": np.array(
			[[0.0 + offset, 0.0, 0.0], [1.0 + offset, 0.0, 0.0], [0.0 + offset, 1.0, 0.0]],
			np.float32,
		),
		"mesh_col": np.array([[255, 0, 0], [0, 255, 0], [0, 0, 255]], np.uint8),
		"mesh_faces": np.array([[0, 1, 2]], np.uint32),
		"obj_cid": np.array([7], np.int32),
		"obj_label": np.array([42], np.uint32),
		"obj_min": np.array([[0.1 + offset, 0.1, 0.0]], np.float32),
		"obj_max": np.array([[0.4 + offset, 0.4, 0.6]], np.float32),
		"place_pos": np.array([[0.0 + offset, 0.0, 0.0], [1.0 + offset, 0.0, 0.0]], np.float32),
		"place_rad": np.array([0.1, 0.2], np.float32),
		"place_edges": np.array([[0, 1]], np.int32),
		"room_pos": np.array([[0.5 + offset, 0.0, 1.0]], np.float32),
		"room_place_edges": np.array([[0, 0]], np.int32),
		"bld_pos": np.array([[0.5 + offset, 0.0, 2.0]], np.float32),
		"bld_room_edges": np.array([[0, 0]], np.int32),
		"obj_place_edges": np.array([[0, 0]], np.int32),
		"agent_pos": np.array([[0.0, 0.0, 0.0], [offset, 0.0, 0.0]], np.float32),
		"agent_quat": np.array([[1.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]], np.float32),
		"agent_t": np.array([0.0, frame / 15.0], np.float64),
		"place_t": np.array([frame / 15.0, frame / 15.0], np.float64),
	}
	np.savez(path / f"snap_{frame:06d}.npz", **arrays)


class ReplayRrdTest(unittest.TestCase):
	def test_rrd_contains_timed_mesh_scene_graph_and_trajectory(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			recording = root / "recording"
			recording.mkdir()
			write_snapshot(recording, 1, 0.1)
			write_snapshot(recording, 5, 0.5)
			output = root / "replay.rrd"

			result = subprocess.run(
				[
					sys.executable,
					str(REPO / "scripts" / "replay.py"),
					"rrd",
					str(recording),
					"--out",
					str(output),
					"--fps",
					"15",
				],
				cwd=REPO,
				capture_output=True,
				text=True,
			)
			self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
			self.assertTrue(output.is_file())
			self.assertGreater(output.stat().st_size, 0)

			verify = subprocess.run(["rerun", "rrd", "verify", str(output)], capture_output=True, text=True)
			self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)

			printed = subprocess.run(
				["rerun", "rrd", "print", "-vvv", str(output)], capture_output=True, text=True, check=True
			).stdout
			for expected in (
				"frame",
				"time",
				"/world/mesh",
				"/world/dsg/objects",
				"/world/dsg/places",
				"/world/dsg/rooms",
				"/world/dsg/buildings",
				"/world/dsg/edges",
				"/world/camera/trajectory",
				"/world/camera/current/forward",
			):
				self.assertIn(expected, printed)
			mesh_rows = 0
			for chunk in printed.split("\nChunk(")[1:]:
				if " - /world/mesh - " not in chunk.splitlines()[0] or "archetype_field: vertex_positions" not in chunk:
					continue
				rows = re.search(r" with (\d+) rows ", chunk.splitlines()[0])
				self.assertIsNotNone(rows)
				mesh_rows += int(rows.group(1))
			self.assertEqual(mesh_rows, 2)


if __name__ == "__main__":
	unittest.main()
