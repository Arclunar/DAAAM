import importlib.util
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
GENERATOR = REPO / "scripts" / "generate_orchestrator_graph.py"


def load_generator():
	if not GENERATOR.exists():
		raise AssertionError(f"missing generator: {GENERATOR}")
	spec = importlib.util.spec_from_file_location("generate_orchestrator_graph", GENERATOR)
	if spec is None or spec.loader is None:
		raise AssertionError(f"cannot load generator: {GENERATOR}")
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


class AstGraphTests(unittest.TestCase):
	def setUp(self) -> None:
		self.temp_dir = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp_dir.cleanup)
		self.source_root = Path(self.temp_dir.name) / "src"
		package = self.source_root / "daaam"
		(package / "pipeline").mkdir(parents=True)
		(package / "__init__.py").write_text("", encoding="utf-8")
		(package / "pipeline" / "__init__.py").write_text("", encoding="utf-8")
		(package / "services.py").write_text(
			textwrap.dedent(
				"""
				class Service:
				    def work(self, value: int) -> int:
				        return abs(value)
				"""
			).lstrip(),
			encoding="utf-8",
		)
		(package / "pipeline" / "runner.py").write_text(
			textwrap.dedent(
				"""
				import math
				from daaam.services import Service

				class Runner:
				    def __init__(self):
				        self.service = Service()

				    def process(self, value: int) -> int:
				        self.helper()
				        if value > 0:
				            self.service.work(value)
				        for item in (value,):
				            math.sqrt(item)
				        self.callback(value)
				        return self.loop(value)

				    def helper(self) -> None:
				        print("helper")

				    def loop(self, value: int) -> int:
				        return self.process(value)
				"""
			).lstrip(),
			encoding="utf-8",
		)

	def test_build_graph_resolves_internal_external_unresolved_and_cycles(self) -> None:
		module = load_generator()
		entry = "daaam.pipeline.runner.Runner.process"

		graph = module.build_graph(self.source_root, entry)
		nodes = {node["qualified_name"]: node for node in graph["nodes"]}

		self.assertEqual(graph["root"], entry)
		self.assertTrue(nodes["daaam.pipeline.runner.Runner.helper"]["expandable"])
		self.assertTrue(nodes["daaam.services.Service.work"]["expandable"])
		self.assertEqual(nodes["math.sqrt"]["kind"], "external")
		self.assertFalse(nodes["math.sqrt"]["expandable"])
		self.assertEqual(nodes["builtins.print"]["kind"], "external")
		self.assertEqual(nodes["self.callback"]["kind"], "unresolved")
		self.assertTrue(
			any(
				edge["source"] == "daaam.pipeline.runner.Runner.loop"
				and edge["target"] == entry
				and edge["cycle"]
				for edge in graph["edges"]
			)
		)

	def test_build_graph_is_deterministic_and_preserves_source_metadata(self) -> None:
		module = load_generator()
		entry = "daaam.pipeline.runner.Runner.process"

		first = module.build_graph(self.source_root, entry)
		second = module.build_graph(self.source_root, entry)
		root = next(node for node in first["nodes"] if node["id"] == entry)

		self.assertEqual(first, second)
		self.assertEqual(root["file"], "daaam/pipeline/runner.py")
		self.assertEqual(root["line_start"], 8)
		self.assertEqual(root["line_end"], 15)
		self.assertEqual(root["signature"], "process(self, value: int) -> int")
		self.assertIn("self.service.work(value)", root["source"])

	def test_edges_preserve_call_order_and_control_flow_context(self) -> None:
		module = load_generator()
		entry = "daaam.pipeline.runner.Runner.process"

		graph = module.build_graph(self.source_root, entry)
		edges = sorted(
			(edge for edge in graph["edges"] if edge["source"] == entry),
			key=lambda edge: edge["order"],
		)

		self.assertEqual([edge["order"] for edge in edges], [1, 2, 3, 4, 5])
		self.assertEqual(
			[edge["expression"] for edge in edges],
			["self.helper", "self.service.work", "math.sqrt", "self.callback", "self.loop"],
		)
		self.assertEqual(edges[0]["context"], [])
		self.assertEqual(edges[1]["context"], ["if value > 0"])
		self.assertEqual(edges[2]["context"], ["for item in (value,)"])


class RealGraphTests(unittest.TestCase):
	def test_real_graph_contains_process_frame_pipeline_calls(self) -> None:
		module = load_generator()

		graph = module.build_graph(REPO / "src", module.DEFAULT_ENTRY_POINT)
		qualified = {node["qualified_name"] for node in graph["nodes"]}
		root = next(node for node in graph["nodes"] if node["id"] == graph["root"])

		self.assertEqual(
			graph["root"],
			"daaam.pipeline.orchestrator.PipelineOrchestrator.process_frame",
		)
		self.assertIn(
			"daaam.pipeline.orchestrator.PipelineOrchestrator._process_tracks",
			qualified,
		)
		self.assertIn(
			"daaam.pipeline.orchestrator.PipelineOrchestrator._add_frame_to_history",
			qualified,
		)
		self.assertIn("daaam.segmentation.services.SegmentationService.segment", qualified)
		self.assertIn("daaam.tracking.services.TrackingService.update", qualified)
		self.assertEqual(root["file"], "daaam/pipeline/orchestrator.py")
		self.assertGreater(root["line_end"], root["line_start"])

	def test_real_graph_describes_the_business_pipeline_in_chinese(self) -> None:
		module = load_generator()
		graph = module.build_graph(REPO / "src", module.DEFAULT_ENTRY_POINT)
		root = next(node for node in graph["nodes"] if node["id"] == graph["root"])
		guide = root["guide"]

		self.assertIn("单帧", guide["purpose"])
		self.assertEqual(guide["inputs"][0]["name"], "frame")
		self.assertEqual(guide["outputs"][0]["name"], "label_image")
		self.assertEqual(guide["outputs"][1]["name"], "color_image")
		self.assertEqual(
			[step["title"] for step in guide["steps"]],
			[
				"登记当前帧",
				"实例分割",
				"跨帧跟踪",
				"构建可融合的轨迹观测",
				"提取可选的视觉语义特征",
				"保存近期帧历史",
				"按周期触发语义分配",
				"生成语义输出图",
				"维护时序状态",
			],
		)
		self.assertEqual(guide["steps"][1]["calls"], [
			"daaam.segmentation.services.SegmentationService.segment"
		])
		self.assertEqual(guide["steps"][6]["kind"], "async")
		self.assertEqual(guide["steps"][8]["kind"], "maintenance")

	def test_real_graph_filters_instrumentation_and_python_plumbing_from_logic_edges(self) -> None:
		module = load_generator()
		graph = module.build_graph(REPO / "src", module.DEFAULT_ENTRY_POINT)
		root_edges = [
			edge for edge in graph["logic_edges"]
			if edge["source"] == graph["root"]
		]

		self.assertEqual(
			[edge["expression"] for edge in root_edges],
			[
				"self.segmentation_service.segment",
				"self.tracking_service.update",
				"self._process_tracks",
				"self.compute_clip_features",
				"self._add_frame_to_history",
				"self._trigger_query_assignment",
				"self._create_output_images",
				"self._check_track_closures",
				"self._cleanup_stale_snapshots",
				"self._send_incremental_position_updates",
			],
		)
		self.assertFalse(any("performance_measure" in edge["expression"] for edge in graph["logic_edges"]))
		self.assertFalse(any("logger." in edge["expression"] for edge in graph["logic_edges"]))

	def test_every_internal_function_has_a_reading_guide(self) -> None:
		module = load_generator()
		graph = module.build_graph(REPO / "src", module.DEFAULT_ENTRY_POINT)

		for node in graph["nodes"]:
			if node["kind"] != "internal":
				continue
			with self.subTest(function=node["id"]):
				guide = node["guide"]
				self.assertTrue(guide["title"])
				self.assertTrue(guide["purpose"])
				self.assertTrue(guide["inputs"])
				self.assertTrue(guide["outputs"])
				self.assertTrue(guide["steps"])
				self.assertNotIn("DAAAM function", guide["purpose"])
				for step in guide["steps"]:
					self.assertTrue(step["description"])
					self.assertTrue(step["source_excerpt"])

	def test_primary_pipeline_explains_intermediate_data(self) -> None:
		module = load_generator()
		graph = module.build_graph(REPO / "src", module.DEFAULT_ENTRY_POINT)
		root = next(node for node in graph["nodes"] if node["id"] == graph["root"])
		steps = {step["title"]: step for step in root["guide"]["steps"]}

		self.assertIn("N×6", steps["实例分割"]["result"])
		self.assertIn("H×W", steps["实例分割"]["result"])
		self.assertIn("M×8", steps["跨帧跟踪"]["result"])
		self.assertIn("Track", steps["构建可融合的轨迹观测"]["result"])
		self.assertIn("semantic_id", steps["生成语义输出图"]["result"])


class HtmlGenerationTests(unittest.TestCase):
	def test_template_renders_embedded_graph_without_remote_resources(self) -> None:
		module = load_generator()
		template = REPO / "scripts" / "orchestrator_graph_viewer.html"
		if not template.exists():
			self.fail(f"missing viewer template: {template}")
		graph = module.build_graph(REPO / "src", module.DEFAULT_ENTRY_POINT)

		html = module.render_html(graph, template)

		self.assertIn("const GRAPH_DATA =", html)
		self.assertIn(graph["root"], html)
		self.assertNotIn(module.DATA_MARKER, html)
		self.assertIsNone(
			re.search(
				r"<(?:script|link|img)[^>]+(?:src|href)=[\"']https?://",
				html,
				flags=re.IGNORECASE,
			)
		)

	def test_render_rejects_a_template_without_exactly_one_data_marker(self) -> None:
		module = load_generator()
		with tempfile.TemporaryDirectory() as temp_dir:
			template = Path(temp_dir) / "template.html"
			template.write_text("<html></html>", encoding="utf-8")

			with self.assertRaisesRegex(ValueError, "exactly one"):
				module.render_html({"root": "root", "nodes": [], "edges": []}, template)


class HtmlInteractionContractTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		module = load_generator()
		graph = module.build_graph(REPO / "src", module.DEFAULT_ENTRY_POINT)
		cls.html = module.render_html(
			graph,
			REPO / "scripts" / "orchestrator_graph_viewer.html",
		)

	def test_page_exposes_graph_navigation_and_inspector_controls(self) -> None:
		for element_id in (
			"search-input",
			"function-list",
			"canvas-viewport",
			"canvas-scene",
			"canvas-connections",
			"relation-panel",
			"breadcrumbs",
			"inspector",
			"detail-panel",
			"source-toggle",
			"expand-level",
			"collapse-all",
			"zoom-in",
			"zoom-out",
			"reset-view",
		):
			with self.subTest(element_id=element_id):
				self.assertIn(f'id="{element_id}"', self.html)

	def test_page_exposes_required_interaction_functions(self) -> None:
		for function_name in (
			"selectFunction",
			"navigateToFunction",
			"renderCanvas",
			"renderFunctionGroup",
			"toggleExpansion",
			"expandOneLevel",
			"collapseAll",
			"setZoom",
			"renderDetailPanel",
			"renderRelations",
			"renderGuide",
		):
			with self.subTest(function_name=function_name):
				self.assertIn(f"function {function_name}", self.html)

	def test_page_distinguishes_canvas_node_types(self) -> None:
		for css_class in (
			"flow-node",
			"flow-node--main",
			"flow-node--optional",
			"flow-node--async",
			"flow-node--maintenance",
			"flow-node--data",
			"flow-arrow",
			"child-branch",
			"function-group",
			"io-grid",
			"purpose-panel",
			"relation-card",
		):
			with self.subTest(css_class=css_class):
				self.assertIn(css_class, self.html)

	def test_page_has_no_force_directed_animation(self) -> None:
		self.assertNotIn("requestAnimationFrame", self.html)
		self.assertNotIn("simulationStep", self.html)
		self.assertNotIn('id="graph-svg"', self.html)

	def test_page_explains_business_logic_instead_of_performance_wrappers(self) -> None:
		self.assertIn("这一步做什么", self.html)
		self.assertIn("输入", self.html)
		self.assertIn("输出", self.html)
		self.assertIn("内部状态变化", self.html)
		self.assertIn("只展示影响处理结果的业务步骤", self.html)
		self.assertIn("中间结果", self.html)
		self.assertIn("逐步解释", self.html)

	def test_page_uses_argos_palette_and_reduced_motion(self) -> None:
		for color in ("#07101a", "#fff5e9", "#ffb56f", "#e78069", "#ffd08b"):
			with self.subTest(color=color):
				self.assertIn(color, self.html)
		self.assertIn("prefers-reduced-motion", self.html)

	def test_detail_panel_has_a_keyboard_accessible_drag_handle(self) -> None:
		self.assertIn('id="detail-resizer"', self.html)
		self.assertIn('role="separator"', self.html)
		self.assertIn('aria-orientation="vertical"', self.html)
		self.assertIn('tabindex="0"', self.html)
		self.assertIn("function initializeDetailResizer", self.html)
		self.assertIn("function setDetailWidth", self.html)

	def test_detail_content_wraps_instead_of_scrolling_horizontally(self) -> None:
		self.assertIn("overflow-x:hidden", self.html)
		self.assertIn("white-space:pre-wrap", self.html)
		self.assertIn("overflow-wrap:anywhere", self.html)

	def test_detail_resizer_remains_available_in_compact_overlay_layout(self) -> None:
		self.assertNotIn(
			"addEventListener('pointerdown',event=>{if(window.innerWidth<=950)return",
			self.html,
		)
		self.assertRegex(
			self.html,
			r"@media\(max-width:950px\).*?\.detail-resizer\{[^}]*position:absolute",
		)


if __name__ == "__main__":
	unittest.main()
