# Unified Replay Workspace Design

**Status:** Approved on 2026-10-16

## Purpose

Replace the generated, self-contained `replay.html` in every mapping result
with one workspace-level replay application backed by a loopback-only local
service. The application can keep several results open in independent tabs,
load their replay data on demand, notice post-processing outputs as they
appear, run post-processing and summarization, and provide contextual
multi-turn scene questions through the existing scene-understanding agent.

The design supports both TUM RGB-D and CODa. It deliberately does not support
the old embedded replay HTML format; a run must be finalized into the new
replay bundle before the central application can open it.

## Problem

The current workflow turns each retained replay recording into a separate HTML
file with all snapshots and images embedded. That has four structural costs:

1. every result contains another copy of the viewer application;
2. large sequences must be decoded and loaded as a single document;
3. later `postprocess` and `summarize` outputs are not reflected without
   rebuilding the page; and
4. `demo_query.py` is a separate CLI, so scene inspection and scene questions
   do not share result selection or visible context.

A browser page opened through `file://` cannot securely enumerate arbitrary
server-side directories, monitor files, run Python jobs, or keep API keys out
of the browser. A local service is therefore part of the architecture rather
than an optional enhancement.

## Goals

- Keep exactly one HTML entry point, `replay.html`, in the workspace.
- Store data, not application code, under each mapping result.
- Discover valid results under `output/` by default and open any absolute path
  visible from WSL.
- Accept WSL paths, `/mnt/<drive>/...`, Windows drive-letter paths and matching
  `\\wsl.localhost\...` paths.
- Support TUM and CODa through one versioned replay bundle contract.
- Load long timelines in bounded chunks instead of one in-memory payload.
- Keep several results open as independent tabs without rendering every tab in
  the background.
- Reflect new post-processing and summary artifacts without rebuilding replay
  data or refreshing the page.
- Run post-processing and summarization from the page with live progress.
- Move the existing scene query workflow into a contextual, persistent,
  multi-turn chat per result.
- Preserve the current interaction rule that ordinary Object/Place selection
  never changes the timeline.
- Allow the current-time camera cards to be repositioned.
- Retain the existing command-line workflows and the optional Rerun artifact.

## Non-goals

- Opening or converting the legacy self-contained per-result `replay.html`.
- Remote or multi-user hosting. The first implementation is loopback-only.
- Side-by-side synchronized comparison of two results. Multiple results use
  tabs, with only the active tab doing heavy rendering.
- Letting the query agent modify files or launch jobs through natural language.
- Replacing the existing post-processing, summarization or scene-query domain
  logic.
- Live visualization while the mapping process is still producing snapshots.
  A mapping run is finalized before it becomes a valid replay bundle.

## Considered Approaches

### A. Central service with a per-result directory bundle — selected

One workspace application talks to a local service. Each result contains a
manifest, independently compressed timeline chunks and required image assets.
Scene graph derivatives stay as normal sibling files and are monitored by the
service.

This gives bounded loading, atomic incremental artifacts, live jobs, hot graph
reload and server-side Agent access without duplicating the application.

### B. Central application with one compressed data file per result

This is simpler to publish but every replay update or partial read requires
replacing or decoding a large monolithic file. It performs poorly for long
sequences and makes recovery from a partial build coarser.

### C. Browser File System Access API with a minimal Agent daemon

This reduces server-side browsing code, but the browser API is not consistently
available, grants are not expressed as stable WSL paths, and background jobs
and file monitoring still require a service. It would create two incompatible
selection models and is rejected.

## Architecture

The system has four layers:

1. **Workspace application** — root `replay.html` plus versioned JS/CSS under
   `scripts/replay_web/static/`.
2. **WSL control service** — directory discovery, path normalization, replay
   data serving, file monitoring, tab/session state, job supervision and SSE.
3. **DAAAM worker environment** — the existing `daaam-ws` container executes
   post-processing, summarization and scene-agent work with its installed
   dependencies and model caches.
4. **Result directories** — immutable finalized replay data plus the normal
   scene graph and summary derivatives.

The control service stays on the WSL host so it can inspect any WSL-visible
path without mounting all of WSL into a container. Heavy jobs receive only the
selected canonical result directory. Agent inputs may be staged into
`.replay-web/cache/`, which is visible to the worker environment, so a shared
long-lived Agent worker can cache models and parsed graphs without a broad host
mount.

`just replay-web` starts the service on `127.0.0.1` and prints the URL of
`/replay.html`. Binding a non-loopback address is rejected by default and is
outside this design.

## Workspace and Result Layout

```text
DAAAM/
├── replay.html
├── scripts/
│   └── replay_web/
│       ├── server.py
│       ├── worker.py
│       └── static/
│           ├── replay.js
│           └── replay.css
├── .replay-web/                 # ignored local state
│   ├── history/
│   ├── jobs/
│   └── cache/
└── output/
    ├── tum_replay/.../
    │   ├── replay/
    │   │   ├── manifest.json
    │   │   ├── timeline/
    │   │   │   ├── 000000.json.gz
    │   │   │   └── ...
    │   │   └── images/
    │   ├── dsg.json
    │   ├── dsg_updated.json
    │   ├── clustered_dsg.json
    │   └── clustered_dsg_with_summaries.json
    └── coda_.../...
```

`replay/` is the only replay bundle name. It is portable with its result: every
image required for current-time camera cards or exact object description-frame
review is copied or encoded under `replay/images/`; the web application never
depends on the original dataset still being mounted.

The optional `replay.rrd` remains a sibling artifact and is not consumed by the
web application.

## Replay Manifest Contract

`replay/manifest.json` is the commit point for a finalized replay bundle. It is
written last through a temporary sibling and atomic rename. A directory without
a valid manifest is not offered as a replay result.

The versioned manifest records at least:

- bundle schema version and generator version;
- creation time and finalized state;
- dataset kind: `tum` or `coda`;
- sequence identifier, source dataset metadata and dataset-specific time origin;
- replay FPS/timebase, first/last timestamp and snapshot count;
- coordinate frame and pose convention;
- available scene graph layers and cameras;
- ordered timeline chunks with relative path, time range, snapshot count,
  uncompressed size and digest;
- image assets or image indexes needed by timeline and description provenance;
- optional reference-mesh metadata;
- source recording/build parameters needed for diagnostics.

All paths inside the manifest are normalized relative paths. Absolute paths,
parent traversal and symlinks that resolve outside the validated result root
are rejected when serving bundle data.

Timeline chunks use independently gzip-compressed JSON payloads initially. Gzip
is available in Python's standard library and modern browsers and avoids a
large JavaScript decompression runtime. The manifest owns the chunk index, so a
later schema may use a binary representation without changing result selection
or server APIs.

## Recording and Finalization

The ZMQ recorder continues to write a private temporary recording during a
mapping run. The existing `build` operation is replaced by a finalization step:

```text
.replay-recording
    -> validate snapshots and timestamps
    -> partition snapshots into bounded timeline chunks
    -> materialize camera/source-frame images
    -> write chunks and indexes atomically
    -> write manifest.json last
```

Finalization writes a fresh temporary `replay/` sibling and atomically publishes
it, so an existing valid bundle remains usable after failure. The temporary raw
recording follows the current retention-on-failure policy. Successful
finalization may remove it unless the existing keep option is enabled.

No result-level HTML is generated. New TUM and CODa workflows both call the
same finalizer and supply dataset-specific manifest fields through explicit
arguments rather than filename inference.

## Result Discovery and Paths

On startup the service scans `output/` recursively to a bounded depth for valid
manifests. The UI can refresh this list or submit another absolute directory.
Opening a path performs these steps:

1. convert a supported Windows drive path to `/mnt/<drive>/...`, or a matching
   WSL UNC path to its Linux path;
2. require an absolute path and resolve `.` components and symlinks;
3. require a readable directory and valid `replay/manifest.json`;
4. validate all referenced bundle files and identify graph derivatives;
5. assign an opaque, process-local `run_id`.

Later browser requests use `run_id`, not a path. After a service restart the
browser restores saved canonical paths and receives new run IDs. Failed opens
are reported in the requesting tab and do not affect already opened results.

## Scene Graph Selection and Hot Reload

For each open result the service selects the most informative valid graph in
this fixed order:

1. `clustered_dsg_with_summaries.json`
2. `clustered_dsg.json`
3. `dsg_updated.json`
4. `dsg.json`

The service polls relevant file metadata at a configurable interval, default
two seconds. Polling is preferred to filesystem-only notifications because WSL,
Windows mounts and Docker writes do not have uniform watcher behaviour. A
changed candidate is parsed and validated before it becomes active. The old
graph remains active when a new file is partial or invalid.

On successful activation, an SSE event includes the filename and version token
(size, nanosecond modification time and content digest when required). The
active tab replaces graph data while retaining timeline position, camera view,
panel layout, visibility choices, selection where possible and chat history. If
the selected entity no longer exists, selection is cleared with an explicit
notice. The next Agent question uses the new graph.

## Web Application and Multi-result Tabs

The page has a directory/open bar, a tab strip, the 3D replay canvas, timeline
controls, movable current-time camera cards, an entity/task/chat sidebar and
status notifications.

Each result tab owns independent:

- replay manifest and chunk cache;
- current time and playback speed;
- 3D camera and layer visibility;
- selected and hidden entities;
- camera-card positions and stacking;
- task display state;
- conversation and Agent model choice.

Only the active tab runs animation, WebGL rendering and camera-image decoding.
Background tabs pause playback and release disposable GPU/image resources, but
remain subscribed to job and artifact events. Closing a tab releases its
client caches but does not delete its files or stored chat history. Tabs can be
reordered and reopened. Side-by-side comparison is not provided.

Opened paths, active tab, tab order and visual layouts are stored in browser
local storage. Conversation content is stored server-side as described below.

## Replay Interaction Rules

- Clicking an Object or Place in either the map or list selects and highlights
  it but never changes the timeline.
- Entity visibility controls remain independent of selection.
- Every object with description provenance has a `描述源帧` action. It opens a
  modal showing the exact recorded frame ID, camera, timestamp and image that
  produced the description. Opening or closing the modal does not change time.
- Missing provenance is represented as unavailable, never guessed from first
  or nearest observation.
- Current-time camera cards can be dragged, brought to front and reset. Their
  positions are clamped to the visible application area and persisted per
  result in browser state.
- Graph hot reload never restarts playback or moves the 3D camera.

## Postprocess and Summarize Jobs

Each tab exposes `Postprocess`, `Summarize` and dependency-aware `全部运行`
actions. Availability derives from manifest dataset metadata and validated
artifacts, not directory names.

The service translates an action into an allowlisted argv template for TUM or
CODa and invokes the existing checked scripts inside `daaam-ws`. Arbitrary web
text is never interpolated into a shell command. For a result outside existing
Compose mounts, the canonical result directory alone is bind-mounted at a
fixed worker path. Dataset-specific place layer, timing and clustering settings
come from the manifest adapter.

Job rules:

- at most one mutating job runs for a given result;
- jobs for different results may run concurrently up to a small configurable
  global limit;
- summarize is enabled only when its required clustered/embedded input exists;
- `全部运行` runs dependencies in order and stops after the first failure;
- rerunning an existing stage requires an explicit confirmation;
- output is built through temporary files and atomically published;
- stdout/stderr and stage/status events stream to clients through SSE;
- cancellation first requests graceful termination, then escalates only for
  the exact recorded child process if it does not stop;
- a service restart marks previously running job records `interrupted`;
- interrupted or failed temporary output never enters graph selection.

Job metadata and bounded logs live under `.replay-web/jobs/`, not in result
directories. Existing `just tum-post`, `tum-summarize`, CODa recipes and direct
scripts remain supported.

## Agent Query Sessions

Each result has a distinct multi-turn query session. The control service sends
the active validated graph and dataset adapter to a long-lived worker in the
existing DAAAM environment. The worker reuses loaded language/embedding models
and keeps an LRU cache of parsed graphs keyed by canonical result identity and
graph version. Concurrency is bounded to prevent multiple tabs from multiplying
GPU/RAM use unexpectedly.

The browser sends:

- the question;
- selected allowlisted model;
- session ID and graph version;
- optionally, current replay time, active camera and selected entity.

The current-view context option is enabled by default and can be disabled. The
Agent's scene tools remain read-only. Job endpoints are separate and are never
made available as Agent tools.

Answers stream to the browser. The visible activity display may name scene
tools and report progress but does not expose hidden prompts, chain-of-thought
or large raw tool payloads. Each completed answer records the model, active
graph filename and graph version used. A graph update inserts a visible version
boundary; it preserves messages but invalidates the worker's old graph cache,
so the next question uses the new graph. A running postprocess job does not
block questions against the last valid graph.

History is stored as JSONL under
`.replay-web/history/<canonical-path-hash>/`, with a small metadata file that
contains the canonical path and schema version. It survives service and browser
restarts without modifying the selected result. `清空对话` clears only the
current result after confirmation. Older content is summarized when a model's
context budget is approached; recent messages and graph-version references are
retained verbatim.

API credentials remain in the server/worker environment and are never returned
to the browser. The model selector is populated from a server-side allowlist
and defaults to the current query script configuration.

## Service API

Exact wire details may be refined without changing these responsibilities:

- `GET /api/config` — CSRF token, limits and allowed Agent models.
- `GET /api/results` — valid results discovered under default `output/`.
- `POST /api/runs/open` — normalize and validate one absolute directory.
- `DELETE /api/runs/{run_id}` — release server-side open-run state only.
- `GET /api/runs/{run_id}/manifest` — validated replay manifest.
- `GET /api/runs/{run_id}/timeline/{chunk}` — one allowlisted chunk.
- `GET /api/runs/{run_id}/images/{asset}` — one manifest-owned image.
- `GET /api/runs/{run_id}/graph` — active normalized graph plus version.
- `GET /api/runs/{run_id}/events` — graph and job SSE stream.
- `POST /api/runs/{run_id}/jobs` — start one allowlisted stage.
- `POST /api/jobs/{job_id}/cancel` — cancel the recorded job.
- `GET /api/runs/{run_id}/chat` — restored conversation.
- `POST /api/runs/{run_id}/chat` — stream one Agent turn.
- `DELETE /api/runs/{run_id}/chat` — clear that result's session.

Mutating routes require a startup CSRF token and same-origin validation. File
responses are derived from validated manifest/artifact entries; the API has no
general-purpose file-read endpoint.

## Error Handling

- Invalid, missing or unsupported manifests produce a tab-local diagnostic with
  the exact expected path and schema; they never crash the service.
- A corrupt timeline chunk stops only that result's playback and identifies the
  chunk. Other open tabs continue operating.
- Missing optional images show placeholders with frame/camera metadata.
- An invalid newer graph is reported while the previous valid graph stays
  active.
- A worker/container startup failure leaves replay browsing available and marks
  only task/query features unavailable.
- Agent timeouts or provider errors append a retryable failed turn without
  discarding conversation history.
- A disconnected SSE client reconnects with the last event ID and then receives
  a current state snapshot to close gaps.
- Browser restoration silently skips paths that no longer exist and presents a
  recoverable notice for each skipped tab.
- All durable service state and generated bundles use temporary writes followed
  by atomic rename where the target filesystem supports it.

## Performance Boundaries

- The manifest and first timeline chunk are sufficient for initial rendering;
  neighboring chunks are prefetched around the playhead.
- Each tab has bounded decoded chunk and image LRU caches.
- Background tabs retain lightweight metadata only and stop animation and image
  decoding.
- Timeline chunk size is chosen by an uncompressed-byte and snapshot-count
  target, not only a fixed number of frames, so dense meshes cannot create an
  unbounded chunk.
- The service supports byte streaming and cache validators for immutable bundle
  assets.
- Graph/Agent caches key on explicit version tokens rather than polling every
  query.
- Multiple Agent turns are queued under the configured worker limit; opening
  multiple tabs does not start one model process per tab.

## Testing Strategy

Implementation follows test-driven development.

### Unit tests

- Windows, WSL, `/mnt` and WSL UNC path normalization;
- canonical-path hashing and result isolation;
- manifest schema, traversal and symlink rejection;
- timeline partitioning and deterministic chunk metadata;
- graph priority and valid-old/invalid-new fallback;
- TUM/CODa adapter selection from manifest fields;
- job state transitions, concurrency and cancellation targeting;
- chat version boundaries, persistence, clearing and context compaction.

### Integration tests

- finalize a small recording and open its bundle through the service;
- request disjoint timeline chunks and camera/source-frame assets;
- publish each graph derivative in turn and observe correct hot selection;
- run fake TUM and CODa postprocess/summarize commands and stream logs;
- simulate partial output, cancellation and service restart;
- switch graph versions while maintaining an Agent session;
- verify two result tabs cannot leak graph, task or chat state.

### Browser tests

- discover the default `output/` list and open an arbitrary path;
- restore, reorder and close multiple result tabs;
- verify inactive tabs stop playback while job updates continue;
- verify map/list selection does not move the timeline;
- open the exact description-frame modal without changing time;
- drag, stack, persist and reset camera cards;
- preserve visual state across a graph hot reload;
- stream and restore chat, display graph provenance and clear one session;
- exercise job confirmation, progress, completion, failure and cancellation.

### End-to-end verification

Run one small TUM and one CODa fixture from replay recording through finalization,
open both as tabs, run postprocess and summarize, observe graph promotion, and
ask a follow-up question that relies on the previous turn. Verify existing CLI
recipes, optional Rerun output and bare `just` behaviour remain unchanged.

## Migration and Documentation

- Replace `scripts/replay.py build` in the run workflow with the bundle
  finalizer; there is no compatibility alias for opening old HTML.
- Stop writing per-result `replay.html` and update command completion messages
  to point to `just replay-web` and the result directory.
- Add `just replay-web` without changing the current default recipe.
- Keep old raw recordings readable only if the new finalizer can consume their
  recording schema; old embedded HTML alone is not an input.
- Document the bundle layout, supported path forms, graph priority, task
  lifecycle, API-key boundary, chat storage and recovery commands.
- Ignore `.replay-web/` local state and document how to remove it safely.
- Record every implementation and documentation change in `CHANGELOG.md` under
  the date it is made.

## Acceptance Criteria

- `just replay-web` serves the sole workspace `replay.html` on loopback.
- No new mapping result contains a generated HTML file.
- A finalized TUM or CODa result opens from the default list or any valid
  WSL-visible absolute path.
- Several results stay open in isolated tabs, and only the active tab performs
  heavy rendering.
- Long replay data is fetched in bounded chunks and required images remain
  available without the source dataset.
- The active graph automatically promotes and demotes according to the approved
  four-file priority while preserving usable UI state.
- Object/Place selection never moves time; description-frame actions show the
  exact source image; camera cards can be repositioned and reset.
- Postprocess and summarize can be started, monitored, cancelled and safely
  rerun from the relevant tab, while existing CLI commands still work.
- Every result has persistent, isolated multi-turn Agent chat whose answers name
  their model and graph version and whose next turn uses a newly activated graph.
- API keys and unrestricted file access never reach the browser.
- Unit, integration, browser and TUM/CODa end-to-end verification pass, and the
  documentation and changelog match the delivered behaviour.
