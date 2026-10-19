"""No-region summaries must fail before a provider call or output publication."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
	import spark_dsg
except ImportError:
	spark_dsg = None


@unittest.skipIf(spark_dsg is None, "requires the DAAAM container")
class EmptyRegionSummaryTest(unittest.TestCase):
	def test_no_assigned_regions_preserves_outputs_and_never_calls_provider(self):
		from click.testing import CliRunner
		from scripts import summarize_regions as summary
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "corrections.yaml").write_text("label_names: []\n")
			graph_output = root / "clustered_dsg_with_summaries.json"
			yaml_output = root / "region_summaries.yaml"
			graph_output.write_text("previous graph")
			yaml_output.write_text("previous summaries")
			graph = SimpleNamespace(num_nodes=lambda: 302)
			with mock.patch.object(summary.sdsg, "DynamicSceneGraph") as loader, \
				mock.patch.object(summary, "objects_by_region", return_value=({}, {}, {})), \
				mock.patch.object(summary, "OpenAI", side_effect=AssertionError("provider must not start")):
				loader.load.return_value = graph
				result = CliRunner().invoke(summary.main, ["--data-dir", directory])
			self.assertNotEqual(result.exit_code, 0)
			self.assertIn("no regions", result.output.lower())
			self.assertEqual(graph_output.read_text(), "previous graph")
			self.assertEqual(yaml_output.read_text(), "previous summaries")
