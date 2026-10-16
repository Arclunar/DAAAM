# `PipelineOrchestrator.process_frame` Interactive Call Graph Design

## Purpose

Create a source-derived, self-contained HTML page that explains what
`PipelineOrchestrator.process_frame()` does. The page presents the call flow as a
dynamic graph: each node is a function, DAAAM-owned calls can be expanded
recursively, and third-party or standard-library calls terminate as clearly
labelled external-dependency leaves.

This feature is documentation tooling only. It must not change the runtime
behaviour of the DAAAM pipeline.

## Deliverables

- `scripts/generate_orchestrator_graph.py`: parses the current Python source and
  generates the documentation page.
- `scripts/orchestrator_graph_viewer.html`: the maintainable HTML/CSS/JavaScript
  template used by the generator.
- `docs/orchestrator_process_frame.html`: the generated single-file page that
  can be opened directly from WSL or Windows without a server.
- `tests/test_generate_orchestrator_graph.py`: focused tests for source analysis,
  graph boundaries, and generated output.
- `just orchestrator-graph`: regenerates the page after source changes.

## Source Analysis

The generator uses only Python's standard-library `ast` module. It indexes
Python definitions and imports beneath `src/daaam`, then starts traversal at
`daaam.pipeline.orchestrator.PipelineOrchestrator.process_frame`.

The resolver supports the call forms needed by the orchestrator:

- calls to methods on `self`, such as `self._process_tracks()`;
- calls through known instance attributes, such as
  `self.segmentation_service.segment()`;
- calls to imported DAAAM functions and classes;
- class-qualified and module-qualified DAAAM calls when their import can be
  resolved statically.

Constructor assignments are used to infer instance-attribute types where the
source makes them explicit. A call that resolves to `src/daaam` becomes an
expandable internal node. A standard-library call, third-party call, built-in,
or statically unresolved dynamic dispatch becomes a non-expandable leaf. The UI
must distinguish external calls from unresolved calls instead of presenting a
guess as fact.

Traversal is deterministic and cycle-safe. Recursive or mutually recursive
edges point back to an existing node and receive a cycle marker rather than
causing infinite expansion. Each node records:

- stable ID, short name, qualified name, and node kind;
- repository-relative file path and source start/end lines;
- signature, docstring or concise source-derived summary, and source excerpt;
- outgoing calls and the call-site line for every edge.

The generator writes the result atomically so a failed regeneration cannot
leave a partially written page.

## Page Interaction and Visual Design

The output is one offline HTML file with all graph data, styles, and JavaScript
inlined. It must not load a CDN, font, analytics script, or other network
resource.

The graph uses SVG with a lightweight force layout. Its initial state shows the
root and direct callees. Clicking an internal node selects it and expands its
next layer; clicking it again can collapse that branch without losing the
current selection. External and unresolved leaf nodes select normally but do
not expand. The user can drag nodes, pan and zoom the canvas, search by function
or qualified name, reset the view, expand or collapse the full internal graph,
and pause the force motion. Breadcrumbs retain the selected path from
`process_frame`.

A fixed inspector on the right shows the selected function's purpose, kind,
signature, file and line, source excerpt, callers, and callees. Edge direction
is visible, and the legend explains internal functions, external dependencies,
unresolved dynamic calls, and cycle edges.

The visual language follows Argos: deep navy background (`#07101a`), warm cream
text (`#fff5e9`), orange primary accent (`#ffb56f`), coral secondary accent
(`#e78069`), gold highlight (`#ffd08b`), and restrained translucent panels.
Nodes drift subtly while the layout settles and react to hover/selection, but
motion must remain functional rather than decorative. The page respects
`prefers-reduced-motion`. On narrow screens the inspector moves below the graph
instead of covering it.

## Data Flow

1. The generator indexes DAAAM modules, imports, classes, functions, and method
   definitions.
2. It resolves calls outward from `process_frame`, classifying each target as
   internal, external, unresolved, or cyclic.
3. It serializes the graph and source metadata into the HTML template.
4. The browser renders the initial neighbourhood and expands already embedded
   graph data locally; browsing never reparses source or makes a network call.
5. After source changes, `just orchestrator-graph` repeats the analysis and
   replaces the generated page.

## Error Handling

Generation fails with a clear non-zero error when the source root, entry point,
or template is missing, or when the output cannot be written. Unsupported call
shapes remain visible as unresolved leaves and include their expression and
call-site line. Syntax errors identify the source file that could not be parsed.
The generator never silently drops an unresolved call from the graph.

## Testing

Implementation follows test-driven development:

1. Small fixture modules verify internal resolution, external leaves,
   constructor-based attribute resolution, cycles, source lines, and stable
   ordering.
2. Repository-level tests generate from the real orchestrator and assert the
   root plus representative direct calls such as `_process_tracks`,
   `_add_frame_to_history`, and the segmentation/tracking service calls.
3. Output tests assert that the document is self-contained, contains no remote
   resource URL, embeds graph data, exposes required controls, and includes the
   Argos colour tokens.
4. A browser smoke check confirms that the generated page opens, renders nodes,
   expands a DAAAM call, selects an external leaf, and updates the inspector.

## Acceptance Criteria

- Opening `docs/orchestrator_process_frame.html` directly displays a usable
  interactive graph without a server or internet connection.
- The initial graph is rooted at `PipelineOrchestrator.process_frame` and its
  direct callees.
- Users can recursively explore only DAAAM-owned functions; external and
  unresolved calls are terminal and visibly distinct.
- Selecting a node shows its role, exact source file and lines, signature,
  excerpt, callers, and callees.
- Search, drag, pan, zoom, expand/collapse, reset, and motion pause work.
- Regeneration reflects current `src/daaam` source and produces deterministic
  output.
- Automated tests pass, the page has no network dependency, and no pipeline
  runtime code is modified.
