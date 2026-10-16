from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class ClusterPlacesAdapterTest(unittest.TestCase):
	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.root = Path(self.temp.name)
		self.original = self.root / "dsg_updated.json"
		self.prepared = self.root / "cluster_input.json"
		self.clustered = self.root / "cluster_output.json"
		self.restored = self.root / "clustered_dsg.json"
		self.place = {
			"id": 7,
			"layer": 3,
			"partition": 0,
			"attributes": {
				"type": "PlaceNodeAttributes",
				"position": [1.0, 2.0, 3.0],
				"color": {"r": 0, "g": 0, "b": 0, "a": 255},
				"distance": 0.3,
				"is_active": True,
				"is_predicted": False,
				"label_weights": [[4, 1.0]],
				"last_update_time_ns": 123,
				"metadata": {},
				"name": "",
				"semantic_feature": {"rows": 0, "cols": 0, "data": None},
				"semantic_label": 4,
				"real_place": True,
				"num_basis_points": 2,
			},
		}
		self.graph = {
			"layer_names": {"PLACES": {"layer": 3, "partition": 0}, "ROOMS": {"layer": 4, "partition": 0}},
			"nodes": [self.place, {"id": 99, "layer": 2, "partition": 0, "attributes": {"type": "SemanticNodeAttributes"}}],
			"edges": [],
		}
		self.original.write_text(json.dumps(self.graph), encoding="utf-8")

	def tearDown(self) -> None:
		self.temp.cleanup()

	def _run(self, *args: str) -> subprocess.CompletedProcess:
		return subprocess.run(
			["python3", str(REPO / "scripts" / "adapt_cluster_places.py"), *args],
			capture_output=True,
			text=True,
		)

	def test_prepare_converts_only_the_selected_place_layer(self) -> None:
		result = self._run("prepare", "--src", str(self.original), "--dst", str(self.prepared), "--layer", "PLACES")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		graph = json.loads(self.prepared.read_text(encoding="utf-8"))
		attrs = graph["nodes"][0]["attributes"]
		self.assertEqual(attrs["type"], "TraversabilityNodeAttributes")
		self.assertEqual(attrs["first_observed_ns"], 123)
		self.assertNotIn("real_place", attrs)
		self.assertEqual(graph["nodes"][1], self.graph["nodes"][1])

	def test_restore_keeps_clustered_rooms_but_restores_place_attributes(self) -> None:
		prepared_result = self._run("prepare", "--src", str(self.original), "--dst", str(self.prepared), "--layer", "PLACES")
		self.assertEqual(prepared_result.returncode, 0, prepared_result.stdout + prepared_result.stderr)
		graph = json.loads(self.prepared.read_text(encoding="utf-8"))
		graph["nodes"][0]["attributes"].update(
			{"distance": 0.8, "is_predicted": True, "color": {"r": 0, "g": 255, "b": 0, "a": 255}}
		)
		room = {"id": 80, "layer": 4, "partition": 0, "attributes": {"type": "RoomNodeAttributes"}}
		graph["nodes"].append(room)
		graph["edges"].append({"source": 80, "target": 7})
		self.clustered.write_text(json.dumps(graph), encoding="utf-8")

		result = self._run(
			"restore",
			"--src",
			str(self.clustered),
			"--original",
			str(self.original),
			"--dst",
			str(self.restored),
			"--layer",
			"PLACES",
		)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		restored = json.loads(self.restored.read_text(encoding="utf-8"))
		attrs = restored["nodes"][0]["attributes"]
		self.assertEqual(attrs["type"], "PlaceNodeAttributes")
		self.assertTrue(attrs["real_place"])
		self.assertEqual(attrs["distance"], 0.8)
		self.assertTrue(attrs["is_predicted"])
		self.assertIn(room, restored["nodes"])
		self.assertIn({"source": 80, "target": 7}, restored["edges"])


if __name__ == "__main__":
	unittest.main()
