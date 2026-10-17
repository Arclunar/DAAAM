# TODO: Unified Replay Workspace Handoff

## Handoff Status

The user approved the architecture and selected **Inline Execution**. The design and the three TDD implementation plans are
committed. Implementation is in progress on the local branch `feat/unified-replay-workspace` (not pushed, `main` is
untouched):

- Phase 1 (unified replay core) and Phase 2 (scene graph hot reload and web-triggered processing) are implemented, covered
  by host, container and real-browser tests, and verified end to end against real TUM and CODa results through the real
  service and Docker (see the commit hashes in the checklist below);
- Phase 3 (Agent chat) is in progress; the checklist below shows which tasks are done.

Do not restart requirements discovery unless the repository or user intent has materially changed. Read the approved
design and plans in full before editing.

## Authoritative Documents

Execute these in order:

1. `docs/superpowers/specs/2026-10-16-unified-replay-workspace-design.md`
2. `docs/superpowers/plans/2026-10-16-unified-replay-core-implementation.md`
3. `docs/superpowers/plans/2026-10-16-replay-web-processing-implementation.md`
4. `docs/superpowers/plans/2026-10-16-replay-web-agent-implementation.md`

The plans contain exact files, test-first steps, interfaces, verification
commands and intended commit boundaries. This TODO is the operational handoff,
not a replacement for those plans.

## User Goal

Replace generated per-result replay pages with one workspace application:

- the repository has a single root `replay.html` entry;
- `just replay-web` starts a loopback-only local service and opens/prints the
  workspace URL;
- the page discovers results under `output/` by default and can open any valid
  WSL-visible absolute result directory;
- several results can remain open as isolated tabs, but only the active tab
  performs heavy rendering;
- post-processing, summarization and multi-turn scene Agent queries are
  available from the page;
- newly published scene-graph artifacts appear without regenerating HTML or
  losing useful UI/chat state.

The user explicitly does **not** want compatibility for old embedded replay
HTML files. Do not add a compatibility alias or continue generating one HTML
file per output directory.

## Settled Architecture

The selected architecture is a central WSL control service plus portable,
versioned replay bundles in result directories:

```text
DAAAM/
├── replay.html
├── scripts/replay_web/
│   ├── server.py
│   ├── worker.py
│   └── static/
│       ├── replay.js
│       └── replay.css
├── .replay-web/                 # ignored local state
│   ├── history/
│   ├── jobs/
│   └── cache/
└── output/.../<result>/
    ├── replay/
    │   ├── manifest.json        # written last; bundle commit point
    │   ├── timeline/*.json.gz
    │   └── images/...
    ├── dsg.json
    ├── dsg_updated.json
    ├── clustered_dsg.json
    └── clustered_dsg_with_summaries.json
```

Important boundaries:

- the service runs on the WSL host and binds `127.0.0.1` only;
- heavy jobs run in the existing `daaam-ws` environment;
- browser APIs use opaque process-local `run_id` values, never raw paths;
- there is no general-purpose file-read or shell-command endpoint;
- mutating routes require same-origin validation and a startup CSRF token;
- `.replay-web/` is local state and must be added to `.gitignore`.

## Result and Bundle Contract

- A result is openable only when `replay/manifest.json` is valid.
- The manifest is written last with an atomic rename.
- Timeline data is split into independently gzip-compressed bounded JSON
  chunks; chunk sizing considers both snapshot count and uncompressed bytes.
- All manifest paths are relative, normalized and contained by the validated
  result root. Reject parent traversal and escaping symlinks.
- Camera-card images and exact description-source images are copied into the
  bundle so browsing does not depend on the source dataset remaining mounted.
- `replay.rrd` remains optional and is not consumed by the web application.
- The service scans `output/` recursively to a bounded depth by default.
- Also accept `/home/...`, `/mnt/c/...`, `C:\\...` and matching WSL UNC paths;
  normalize and validate them before registering a result.

## Dataset Behaviour

Both TUM and CODa must be supported through explicit manifest adapters, not
directory-name guessing.

- TUM keeps the full recorded replay timeline.
- Current CODa runs have no historical ZMQ replay recorder. New CODa bags must
  write a `daaam_replay.json` sidecar with dataset kind, sequence, start frame,
  frame count, FPS and time origin.
- CODa finalization initially produces a valid one-snapshot replay bundle from
  the final `dsg.json`; do not add a costly live recorder merely to imitate the
  TUM timeline.
- CODa RGB files require numeric ordering (`..._1.png`, `..._10.png`, etc.).
- Existing CLI recipes (`tum-post`, `tum-summarize`, CODa recipes and scripts)
  must continue working after web controls are added.

## Scene Graph Selection and Hot Reload

Use exactly this priority:

1. `clustered_dsg_with_summaries.json`
2. `clustered_dsg.json`
3. `dsg_updated.json`
4. `dsg.json`

Poll candidate metadata (default every two seconds), parse and validate a
changed candidate before activation, and retain the last valid graph when a
new file is partial or invalid. Publish graph changes through SSE with an
explicit version token.

On activation preserve timeline position, 3D camera, layouts, visibility,
selection where the entity still exists and chat history. Clear a missing
selection with a visible notice. The next Agent turn must use the new graph.

## Replay Interaction Rules

- Clicking an Object or Place in the map or list selects/highlights it and
  never changes the timeline.
- Every object with exact description provenance has a `描述源帧` action that
  opens its recorded frame ID, camera, timestamp and image in a modal without
  moving time. Missing provenance stays unavailable; never guess it.
- Current-time camera cards are draggable, can be raised and reset, are
  clamped to the application area and persist per result.
- Multiple results use tabs only; no side-by-side layout is required.
- Each tab independently owns time, camera, layer/entity visibility, selection,
  camera-card layout, tasks and conversation.
- Background tabs pause playback/rendering/image decoding but continue
  monitoring artifact and job events.

## Processing and Summarization

The web UI provides `Postprocess`, `Summarize` and dependency-aware `全部运行`.
The service maps each action to an allowlisted argv template; never interpolate
browser text into a shell command.

Required job semantics:

- at most one mutating job per result;
- small bounded concurrency across different results;
- summarize is enabled only when its prerequisite output exists;
- `全部运行` stops on the first failed dependency;
- rerunning an existing stage requires explicit confirmation;
- stage outputs publish atomically from temporary files;
- bounded logs/status stream through SSE;
- cancellation targets only the recorded child process, graceful first;
- service restart marks unfinished jobs `interrupted`;
- failed/interrupted temporary output never participates in graph selection.

Persist job metadata and bounded logs under `.replay-web/jobs/`, not inside the
result directory.

## Agent Chat

Provide one persistent multi-turn conversation per canonical result path.

- A long-lived worker in the DAAAM environment owns model imports and an LRU
  cache of parsed graphs/Agent instances.
- The WSL control service must not import `spark_dsg` or model libraries.
- Use an NDJSON worker protocol with `inspect`, `ask` and `invalidate` actions.
- History is JSONL under `.replay-web/history/<canonical-path-hash>/` and
  survives browser/service restarts.
- History roles include `user`, `assistant`, `boundary` and `error`; only
  suitable user/assistant content is passed back to providers.
- Each answer records its model, active graph filename and version.
- A graph update inserts a visible version boundary and invalidates the old
  worker cache without deleting the conversation.
- `清空对话` affects only the current result after confirmation.
- The UI may show safe tool names and progress, but private prompts, hidden
  reasoning/chain-of-thought and large raw tool payloads must never reach the
  browser or persistent history.
- API credentials remain in the server/worker environment.

## Implementation Checklist

### Phase 1 — Unified replay core

Follow `2026-10-16-unified-replay-core-implementation.md`:

- [x] Task 1: define and validate the replay bundle manifest. (`7cac138`)
- [x] Task 2: finalize recordings into chunked replay bundles. (`d1fb283`)
- [x] Task 3: switch TUM/CODa production from per-result HTML to bundles. (`2d3504f`)
- [x] Task 4: normalize arbitrary result paths and register open runs. (`77e27ab`)
- [x] Task 5: serve the root application and immutable bundle assets. (`a096e21`)
- [x] Task 6: implement isolated multi-result tabs and workspace persistence. (`b9990c4`)
- [x] Task 7: port the existing WebGL viewer to HTTP chunk loading, then remove
      `scripts/replay_viewer.html` only after parity is tested. (`aa6b007`; the
      `build` subcommand was removed here rather than in Task 3, so that every
      intermediate commit kept its browser tests green)
- [x] Task 8: implement draggable, tab-aware current-time camera cards. (`2cd9c8d`)
- [x] Task 9: document and verify the core migration end to end. (`0c2179f`)

### Phase 2 — Processing and hot reload

Follow `2026-10-16-replay-web-processing-implementation.md`:

- [x] Task 1: select and monitor the active scene graph. (`08c47d2`)
- [x] Task 2: publish graph changes through SSE and apply them per tab. (`c86dcbe`)
- [x] Task 3: make postprocess/summarize stage outputs atomic. (`13e4f4e`)
- [x] Task 4: supervise allowlisted processing jobs. (`f8559b1`)
- [x] Task 5: expose job APIs and the per-tab task panel. (`57c6c30`; cancelling was corrected in `1e4428b` after a real Docker run showed that ending the client left the container running)
- [x] Task 6: verify TUM and CODa workflows and document recovery. (`da8a12e`)

### Phase 3 — Agent chat

Follow `2026-10-16-replay-web-agent-implementation.md`:

- [x] Task 1: add provider-neutral conversation context and progress hooks.
- [ ] Task 2: load TUM/CODa query graphs and cache Agent instances.
- [ ] Task 3: persist isolated histories and graph boundaries.
- [ ] Task 4: run one long-lived Agent worker through NDJSON.
- [ ] Task 5: stream per-tab chat through the service and browser.
- [ ] Task 6: verify persistent multi-turn behaviour and document operations.

## Required Working Method

- This is a large implementation. Use an isolated worktree/branch before code
  changes; the approved plans assume test-driven, reviewable batches.
- Follow each plan's red/green/refactor order: add the named failing test, run
  it and observe the expected failure, implement the minimum behaviour, rerun
  the scoped tests, then run the wider relevant suite.
- Preserve unrelated user changes and inspect `git status` before every batch.
- Use Python 3.8-compatible annotations in container-side code (`typing.Dict`,
  `List`, `Tuple`, etc.; do not rely on newer-only syntax).
- Keep bare `just` behaviour unchanged.
- Record **every** repository modification, including docs/config changes, in
  `CHANGELOG.md` under the actual current date.
- Do not claim end-to-end success from unit tests alone. Perform TUM and CODa
  verification described by the plans, or report exactly what remains blocked.

## Existing Code to Reuse

- `scripts/replay.py`: snapshot extraction, packing, quantization, reference
  mesh, description provenance/images, recording, RRD and old HTML build.
- `scripts/replay_viewer.html`: existing WebGL UI and interaction behaviour to
  port before deletion.
- `scripts/run_replay.sh`: current TUM recording/build workflow.
- `scripts/run_coda_example.sh` and `scripts/make_coda_bag.sh`: CODa execution
  and frame-range information.
- `scripts/postprocess_coda.sh`, `scripts/postprocess_scene_graph.py` and
  `scripts/summarize_regions.py`: current postprocess/summarize stages.
- `src/daaam/scene_understanding/services.py`: current single-turn Agent entry.
- Replay and workflow regression tests under `tests/test_replay_*.py`,
  `tests/test_run_replay.py`, `tests/test_coda_*.py` and
  `tests/test_justfile.py`.

## Known Pitfalls and Superseded Notes

- `TODO_REPLAY_TSDF.md` is a separate older approved feature. Its description
  assumes self-contained per-result HTML. The unified workspace decision is
  newer and authoritative: if that mesh/label work is resumed, adapt it to the
  central viewer and replay bundle rather than restoring per-result HTML.
- Do not infer dataset kind, sequence, timing or place layer from result folder
  names; use validated manifest metadata.
- Do not expose arbitrary host paths after open. Only serve allowlisted bundle
  and artifact entries through `run_id`.
- Do not let a partial postprocess output become the active graph.
- Do not spawn one heavy model process per browser tab.
- Do not treat background tabs as inactive for job/graph monitoring.
- Do not expose Agent `reasoning` fields even if a provider returns them.

## Definition of Done

- `just replay-web` serves the sole root `replay.html` on loopback.
- New TUM/CODa results contain a valid portable `replay/` bundle and no
  generated HTML.
- Default discovery and all approved absolute path forms work.
- Multiple isolated result tabs work while only the active tab renders.
- Chunked timelines, bundled images, stable selection behaviour and draggable
  camera cards work.
- Scene graph priority/hot reload works without moving time or losing useful
  state.
- Postprocess/summarize can be started, observed, cancelled and safely rerun.
- Persistent multi-turn Agent chat uses the current valid graph and records its
  model/version without leaking secrets or hidden reasoning.
- Unit, integration, browser and real TUM/CODa checks described in the plans
  pass, and `USAGE.md`, relevant command help and `CHANGELOG.md` match the
  delivered behaviour.

## Recommended First Actions for the Next AI

1. Read the four authoritative documents above in full.
2. Re-check `git status`, current branch and any concurrent changes.
3. Create/verify an isolated worktree and implementation branch.
4. Start only Phase 1, Task 1 by writing the manifest validation tests first.
5. Execute in small batches and stop for review at the checkpoints in the
   plans; do not jump ahead to processing or Agent work before the core bundle
   and service contracts are stable.
