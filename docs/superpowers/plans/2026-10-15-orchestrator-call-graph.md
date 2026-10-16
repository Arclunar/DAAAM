# Orchestrator Call Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a regenerable, self-contained interactive HTML call graph rooted at `PipelineOrchestrator.process_frame`.

**Architecture:** A standard-library Python AST analyzer indexes `src/daaam`, resolves DAAAM-owned calls recursively, and classifies everything else as an external or unresolved terminal node. A separate offline HTML template embeds the deterministic graph JSON and renders it as an interactive SVG force graph with a fixed source inspector.

**Tech Stack:** Python 3 standard library (`ast`, `argparse`, `json`, `pathlib`, `tempfile`, `unittest`), HTML5, CSS, vanilla JavaScript, SVG.

**Spec:** `docs/superpowers/specs/2026-10-15-orchestrator-call-graph-design.md`

## Global Constraints

- Do not modify DAAAM pipeline runtime code.
- Parse only repository Python source; do not import DAAAM modules during generation.
- Expand only definitions under `src/daaam`; external and unresolved calls are leaf nodes.
- Emit a deterministic, single-file HTML page with no network dependency.
- Use the Argos palette: `#07101a`, `#fff5e9`, `#ffb56f`, `#e78069`, and `#ffd08b`.
- Record all repository changes under `2026-10-15` in `CHANGELOG.md`.

---

### Task 1: AST Index and Call Resolution

**Files:**
- Create: `scripts/generate_orchestrator_graph.py`
- Create: `tests/test_generate_orchestrator_graph.py`

**Interfaces:**
- Produces: `SourceIndex(source_root: Path)`, `SourceIndex.build() -> None`, and `SourceIndex.resolve_entry(qualified_name: str) -> Definition`.
- Produces: `build_graph(source_root: Path, entry_point: str) -> dict[str, object]` with top-level `root`, `nodes`, and `edges` keys.
- Each node provides `id`, `name`, `qualified_name`, `kind`, `file`, `line_start`, `line_end`, `signature`, `summary`, `source`, and `expandable`.
- Each edge provides `source`, `target`, `line`, `expression`, and `cycle`.

- [ ] **Step 1: Write fixture-based failing tests**

Create temporary `src/daaam` modules that cover a local helper, a constructor-bound service method, `math.sqrt`, a dynamic callback, and a recursion cycle. Assert exact classifications and stable ordering:

```python
graph = module.build_graph(source_root, "daaam.pipeline.runner.Runner.process")
nodes = {node["qualified_name"]: node for node in graph["nodes"]}
self.assertTrue(nodes["daaam.pipeline.runner.Runner.helper"]["expandable"])
self.assertEqual(nodes["math.sqrt"]["kind"], "external")
self.assertEqual(nodes["callback"]["kind"], "unresolved")
self.assertTrue(any(edge["cycle"] for edge in graph["edges"]))
self.assertEqual(graph, module.build_graph(source_root, entry))
```

- [ ] **Step 2: Run the focused tests and confirm the expected failure**

Run: `python3 -m unittest tests.test_generate_orchestrator_graph.AstGraphTests -v`

Expected: FAIL because `scripts/generate_orchestrator_graph.py` and its API do not exist.

- [ ] **Step 3: Implement the minimal source index**

Define immutable records for modules, definitions, and calls. Index `FunctionDef`, `AsyncFunctionDef`, `ClassDef`, `Import`, and `ImportFrom`; retain source with `ast.get_source_segment`; normalize module paths from `source_root`.

```python
@dataclass(frozen=True)
class Definition:
    qualified_name: str
    module: str
    class_name: str | None
    name: str
    path: Path
    node: ast.FunctionDef | ast.AsyncFunctionDef

class SourceIndex:
    def __init__(self, source_root: Path): ...
    def build(self) -> None: ...
    def resolve_entry(self, qualified_name: str) -> Definition: ...
```

- [ ] **Step 4: Implement deterministic call resolution and graph traversal**

Resolve `self.method`, imported symbols, imported modules, explicit class calls, and `self.<attribute>.method` using assignments in the containing class constructor. Create external leaves for imports outside `daaam`, unresolved leaves for dynamic expressions, reuse node IDs for repeated calls, and mark back-edges into the active traversal stack as cycles.

```python
def build_graph(source_root: Path, entry_point: str) -> dict[str, object]:
    index = SourceIndex(source_root)
    index.build()
    root = index.resolve_entry(entry_point)
    return GraphBuilder(index).build(root)
```

- [ ] **Step 5: Run the fixture suite until it passes**

Run: `python3 -m unittest tests.test_generate_orchestrator_graph.AstGraphTests -v`

Expected: all `AstGraphTests` pass and two repeated builds compare equal.

### Task 2: Real-Orchestrator Coverage and HTML Generation

**Files:**
- Modify: `scripts/generate_orchestrator_graph.py`
- Create: `scripts/orchestrator_graph_viewer.html`
- Modify: `tests/test_generate_orchestrator_graph.py`

**Interfaces:**
- Consumes: `build_graph(source_root, entry_point)` from Task 1.
- Produces: `render_html(graph: dict[str, object], template_path: Path) -> str`.
- Produces CLI arguments `--source-root`, `--template`, `--output`, and `--entry-point`.
- Template consumes exactly one marker, `/*__DAAAM_GRAPH_DATA__*/`, replaced by `const GRAPH_DATA = <JSON>;`.

- [ ] **Step 1: Add failing repository and rendering tests**

Assert the real graph contains the root and representative calls, embeds exact source locations, and renders without remote resources:

```python
graph = module.build_graph(REPO_ROOT / "src", module.DEFAULT_ENTRY_POINT)
qualified = {node["qualified_name"] for node in graph["nodes"]}
self.assertIn("daaam.pipeline.orchestrator.PipelineOrchestrator._process_tracks", qualified)
self.assertIn("daaam.pipeline.orchestrator.PipelineOrchestrator._add_frame_to_history", qualified)
self.assertTrue(any("SegmentationService.segment" in name for name in qualified))

html = module.render_html(graph, REPO_ROOT / "scripts/orchestrator_graph_viewer.html")
self.assertIn("const GRAPH_DATA =", html)
self.assertNotRegex(html, r"https?://")
```

- [ ] **Step 2: Run the new tests and confirm they fail**

Run: `python3 -m unittest tests.test_generate_orchestrator_graph.RealGraphTests tests.test_generate_orchestrator_graph.HtmlGenerationTests -v`

Expected: FAIL because the template and rendering/CLI functions do not exist.

- [ ] **Step 3: Implement the self-contained template shell**

Create semantic toolbar, graph viewport, SVG layers, legend, breadcrumb trail, status region, and right-side inspector. Include the five required Argos colour tokens, responsive layout, accessible labels, keyboard focus styling, and a reduced-motion media query.

- [ ] **Step 4: Implement safe rendering and atomic CLI output**

Escape JSON closing tags before inserting it, require exactly one marker, and replace the output atomically:

```python
def render_html(graph: dict[str, object], template_path: Path) -> str:
    payload = json.dumps(graph, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    template = template_path.read_text(encoding="utf-8")
    if template.count(DATA_MARKER) != 1:
        raise ValueError(f"template must contain exactly one {DATA_MARKER}")
    return template.replace(DATA_MARKER, f"const GRAPH_DATA = {payload};")
```

Write to a temporary sibling and use `Path.replace()` only after successful rendering.

- [ ] **Step 5: Run the complete Python test file**

Run: `python3 -m unittest tests.test_generate_orchestrator_graph -v`

Expected: all analysis and generation tests pass.

### Task 3: Interactive SVG Graph

**Files:**
- Modify: `scripts/orchestrator_graph_viewer.html`
- Modify: `tests/test_generate_orchestrator_graph.py`

**Interfaces:**
- Consumes: embedded `GRAPH_DATA.nodes`, `GRAPH_DATA.edges`, and `GRAPH_DATA.root`.
- Produces browser functions `setSelectedNode(id)`, `toggleNode(id)`, `setAllExpanded(expanded)`, `fitGraph()`, `setSimulationPaused(paused)`, and `render()`.
- Exposes stable DOM IDs `graph-svg`, `search-input`, `expand-all`, `collapse-all`, `reset-view`, `pause-motion`, `breadcrumbs`, and `inspector`.

- [ ] **Step 1: Add failing static interaction-contract tests**

Check required DOM IDs, named functions, node-kind classes, arrow markers, source panel, and reduced-motion CSS:

```python
for token in ("graph-svg", "search-input", "expand-all", "collapse-all", "reset-view", "pause-motion", "breadcrumbs", "inspector"):
    self.assertIn(f'id="{token}"', html)
for function_name in ("setSelectedNode", "toggleNode", "setAllExpanded", "fitGraph", "setSimulationPaused", "render"):
    self.assertIn(f"function {function_name}", html)
self.assertIn("prefers-reduced-motion", html)
```

- [ ] **Step 2: Run the interaction-contract test and confirm it fails**

Run: `python3 -m unittest tests.test_generate_orchestrator_graph.HtmlInteractionContractTests -v`

Expected: FAIL until the graph interactions are implemented.

- [ ] **Step 3: Implement graph state and one-layer expansion**

Initialize `expanded` with only the root, derive visible nodes/edges from reachable expanded parents, select and expand internal nodes on click, and keep external/unresolved leaves terminal. Render breadcrumbs from the selected node's shortest visible parent chain.

- [ ] **Step 4: Implement force motion, drag, pan, zoom, and controls**

Use `requestAnimationFrame` with repulsion, spring, centering, and collision forces. Store transform and pointer state separately from node positions. Connect search, full expand/collapse, view reset, fit, and motion pause controls. Respect `matchMedia('(prefers-reduced-motion: reduce)')` on first load.

- [ ] **Step 5: Implement the inspector and visual states**

Render purpose, kind, signature, repository-relative path, exact line range, source excerpt, callers, and callees with text nodes or escaped content. Style internal, external, unresolved, selected, search-match, and cyclic states using the Argos palette.

- [ ] **Step 6: Run the interaction-contract and full generator suites**

Run: `python3 -m unittest tests.test_generate_orchestrator_graph -v`

Expected: all tests pass.

### Task 4: Repository Entry Point, Generated Artifact, and Verification

**Files:**
- Modify: `justfile`
- Create: `docs/orchestrator_process_frame.html` (generated)
- Modify: `tests/test_justfile.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes the generator CLI from Task 2.
- Produces `just orchestrator-graph`, which regenerates `docs/orchestrator_process_frame.html` and prints both WSL and Windows-openable paths when `wslpath` is available.

- [ ] **Step 1: Add a failing recipe test**

Extend the existing `tests/test_justfile.py` style to assert that `just --dry-run orchestrator-graph` invokes the generator with the repository source, template, and documented output path.

- [ ] **Step 2: Run the recipe test and confirm it fails**

Run: `python3 -m unittest tests.test_justfile -v`

Expected: FAIL because the recipe does not exist.

- [ ] **Step 3: Add the recipe and generate the checked-in page**

Add:

```just
# 重新分析当前源码并生成可离线打开的 process_frame 函数调用图
orchestrator-graph:
    python3 scripts/generate_orchestrator_graph.py --source-root src --template scripts/orchestrator_graph_viewer.html --output docs/orchestrator_process_frame.html
```

Run: `just orchestrator-graph`

Expected: `docs/orchestrator_process_frame.html` is replaced atomically and the command prints its absolute path.

- [ ] **Step 4: Update the changelog to describe the completed feature**

Replace the design-only entry with one `Added` entry covering the generator, recursive DAAAM call graph, external leaves, inspector, offline HTML, tests, and regeneration command. Do not list generated-file mechanics.

- [ ] **Step 5: Run all targeted tests and deterministic-generation check**

Run:

```bash
python3 -m unittest tests.test_generate_orchestrator_graph tests.test_justfile -v
sha256sum docs/orchestrator_process_frame.html
just orchestrator-graph
sha256sum docs/orchestrator_process_frame.html
```

Expected: all tests pass and both hashes are identical.

- [ ] **Step 6: Run browser smoke verification**

Open `docs/orchestrator_process_frame.html`, confirm the root and direct calls render, expand `_process_tracks`, select an external dependency, search for `_create_output_images`, inspect its file/lines/source, exercise drag/pan/zoom and pause, and verify the narrow responsive layout. Record any browser console error as a failed check.

- [ ] **Step 7: Review the final change scope**

Run: `git status --short` and inspect diffs only for the plan's files. Confirm `CHANGELOG.md` accurately describes all modifications and that no existing unrelated change was altered.
