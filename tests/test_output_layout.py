from __future__ import annotations

import ast
import importlib.util
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class OutputLayoutTest(unittest.TestCase):
	def _load_output_module(self):
		path = REPO / "src" / "daaam" / "utils" / "output.py"
		spec = importlib.util.spec_from_file_location("daaam_output", path)
		module = importlib.util.module_from_spec(spec)
		assert spec.loader is not None
		spec.loader.exec_module(module)
		return module

	def test_an_explicit_run_directory_is_used_without_another_timestamp_layer(self) -> None:
		output = self._load_output_module()
		with tempfile.TemporaryDirectory() as temp:
			run = Path(temp) / "output" / "tum_replay" / "out_20261016_120000"
			resolved = output.resolve_pipeline_output_dir(
				root_dir=Path(temp),
				configured_output_dir="ignored",
				exact_output_dir=run,
				timestamp="20261016_120001",
			)
			self.assertEqual(resolved, run)

	def test_standalone_orchestrator_keeps_its_timestamped_output(self) -> None:
		output = self._load_output_module()
		with tempfile.TemporaryDirectory() as temp:
			resolved = output.resolve_pipeline_output_dir(
				root_dir=Path(temp),
				configured_output_dir="output/custom",
				timestamp="20261016_120001",
			)
			self.assertEqual(resolved, Path(temp) / "output/custom/out_20261016_120001")

	def test_hydra_runner_passes_its_run_directory_to_the_orchestrator(self) -> None:
		tree = ast.parse((REPO / "src" / "daaam" / "hydra" / "runner.py").read_text(encoding="utf-8"))
		calls = [
			node
			for node in ast.walk(tree)
			if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "PipelineOrchestrator"
		]
		self.assertEqual(len(calls), 1)
		keywords = {keyword.arg: keyword.value for keyword in calls[0].keywords}
		value = keywords.get("output_dir")
		self.assertIsInstance(value, ast.Attribute)
		self.assertIsInstance(value.value, ast.Name)
		self.assertEqual((value.value.id, value.attr), ("self", "output_dir"))


if __name__ == "__main__":
	unittest.main()
