# Replay Web Processing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make open replay tabs automatically adopt the best valid scene graph and safely run postprocess or summarize jobs with live status from the web page.

**Architecture:** A polling artifact monitor validates graph candidates in the approved priority order and publishes versioned events through a replay-wide SSE broker. A bounded job manager executes only allowlisted Docker argv for the selected canonical result, while existing processing scripts gain stage selection and atomic outputs; the frontend adds graph-version and task panels without disturbing replay state.

**Tech Stack:** Python 3.8+ standard library, JSON/SSE, subprocess/Docker Compose, Bash, ROS 2 launch, plain JavaScript, unittest, Selenium.

**Spec:** `docs/superpowers/specs/2026-10-16-unified-replay-workspace-design.md`

## Global Constraints

- Execute this plan only after `2026-10-16-unified-replay-core-implementation.md` is complete.
- Select graphs strictly in this order: `clustered_dsg_with_summaries.json`, `clustered_dsg.json`, `dsg_updated.json`, `dsg.json`.
- Poll relevant files every two seconds by default; do not depend solely on inotify/watchdog across WSL, `/mnt`, and Docker writes.
- Parse and validate a changed graph before activation; keep the previous valid graph when a candidate is partial or invalid.
- Graph activation must preserve time, 3D camera, visibility, camera-card layout, tab state and chat history.
- One result may have only one mutating job; global job concurrency defaults to one and is configurable.
- Browser input selects only an allowlisted stage. Never construct a shell command from browser text.
- Reruns require explicit confirmation; temporary outputs must never become active artifacts.
- Existing `tum-post`, `tum-summarize`, `coda-post`, and `coda-summarize` behaviour must remain available.
- Every repository modification must be covered accurately by the `2026-10-16` changelog before its task commit.

---

### Task 1: Select and monitor the active scene graph

**Files:**
- Create: `scripts/replay_web/artifacts.py`
- Create: `tests/test_replay_artifacts.py`
- Modify: `scripts/replay_web/runs.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: canonical `OpenRun` records from the core plan.
- Produces: `GRAPH_PRIORITY: Tuple[str, ...]` equal to the approved order.
- Produces: immutable `ArtifactVersion(name, path, size, mtime_ns, digest)`.
- Produces: `load_valid_graph(path: Path) -> Tuple[Dict[str, Any], ArtifactVersion]`.
- Produces: `select_active_graph(result_dir: Path) -> Optional[Tuple[Dict[str, Any], ArtifactVersion]]`.
- Produces: `ArtifactMonitor(registry, callback, interval=2.0)` with `start()`, `stop()`, and `scan_once()`.

- [ ] **Step 1: Write priority, fallback and change-detection tests**

```python
def test_best_valid_graph_wins_and_invalid_newer_candidate_does_not_replace_it(self):
    write_graph(result / "dsg.json", marker="raw")
    write_graph(result / "clustered_dsg.json", marker="clustered")
    graph, version = select_active_graph(result)
    self.assertEqual((graph["marker"], version.name), ("clustered", "clustered_dsg.json"))
    (result / "clustered_dsg_with_summaries.json").write_text("{")
    graph, version = select_active_graph(result)
    self.assertEqual((graph["marker"], version.name), ("clustered", "clustered_dsg.json"))
```

Add tests for promotion, demotion after deletion, same-size/same-mtime content
change detected by digest, no candidate, malformed/non-object JSON, callback
only after version change, and one run's failure not stopping another run scan.

- [ ] **Step 2: Verify missing monitor failures**

Run: `python3 -m unittest tests.test_replay_artifacts -v`

Expected: FAIL importing `scripts.replay_web.artifacts`.

- [ ] **Step 3: Implement validation and polling**

```python
GRAPH_PRIORITY = (
    "clustered_dsg_with_summaries.json",
    "clustered_dsg.json",
    "dsg_updated.json",
    "dsg.json",
)

def load_valid_graph(path: Path) -> Tuple[Dict[str, Any], ArtifactVersion]:
    stat = path.stat()
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("nodes"), list):
        raise ArtifactError(f"{path.name} is not a scene graph JSON object")
    version = ArtifactVersion(path.name, path, stat.st_size, stat.st_mtime_ns, hashlib.sha256(raw).hexdigest())
    return value, version
```

Keep `last_good[run_id]` and `last_error[(run_id, filename)]`; emit one error
callback per changed invalid version, not every poll. If a higher candidate is
invalid, continue down the priority list. If the active file disappears,
activate the next valid candidate and emit a demotion event.

- [ ] **Step 4: Run focused tests and commit**

Run: `python3 -m unittest tests.test_replay_artifacts tests.test_replay_web_runs -v`

Expected: PASS.

```bash
git add CHANGELOG.md scripts/replay_web/artifacts.py scripts/replay_web/runs.py tests/test_replay_artifacts.py
git commit -m "feat: monitor replay scene graph artifacts"
```

---

### Task 2: Publish graph changes through SSE and apply them per tab

**Files:**
- Create: `scripts/replay_web/events.py`
- Create: `tests/test_replay_events.py`
- Modify: `scripts/replay_web/server.py`
- Modify: `tests/test_replay_web_server.py`
- Modify: `scripts/replay_web/static/api.js`
- Modify: `scripts/replay_web/static/app.js`
- Modify: `scripts/replay_web/static/viewer.js`
- Modify: `scripts/replay_web/static/replay.css`
- Modify: `tests/test_replay_workspace_browser.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: ArtifactMonitor callbacks from Task 1.
- Produces: `EventBroker.publish(run_id, event_type, payload) -> ReplayEvent` and `subscribe(run_id, after_id) -> EventSubscription`.
- Produces: `GET /api/runs/{run_id}/graph` and `GET /api/runs/{run_id}/events`.
- Produces: `ReplayApi.graph(runId)` and `ReplayApi.events(runId, handlers)`.
- Produces: `ReplayViewer.applyGraph(graphDocument, graphVersion)` without resetting replay state.

- [ ] **Step 1: Write bounded event replay and HTTP SSE tests**

```python
broker = EventBroker(history_limit=3)
events = [broker.publish("run", "graph", {"n": n}) for n in range(5)]
with broker.subscribe("run", after_id=events[2].event_id) as subscription:
    self.assertEqual([subscription.get(timeout=.1).payload["n"] for _ in range(2)], [3, 4])
```

Test monotonically increasing IDs, separate run channels, slow-subscriber queue
replacement with a current-state event, `Last-Event-ID`, heartbeat comments,
disconnect cleanup, graph endpoint provenance, and server shutdown unblocking
subscriptions.

- [ ] **Step 2: Run and observe missing event support**

Run: `python3 -m unittest tests.test_replay_events tests.test_replay_web_server -v`

Expected: FAIL for the new API.

- [ ] **Step 3: Implement the broker and SSE route**

```python
@dataclass(frozen=True)
class ReplayEvent:
    event_id: int
    run_id: str
    event_type: str
    payload: Mapping[str, Any]

def write_sse(handler, event: ReplayEvent) -> None:
    handler.wfile.write(f"id: {event.event_id}\n".encode())
    handler.wfile.write(f"event: {event.event_type}\n".encode())
    handler.wfile.write(("data: " + json.dumps(event.payload, separators=(",", ":")) + "\n\n").encode())
    handler.wfile.flush()
```

Give each subscription a bounded queue. On overflow, drop queued deltas and
enqueue one `state` snapshot. The server starts/stops ArtifactMonitor with its
lifecycle and publishes `graph`, `artifact-error`, and `state` events.

- [ ] **Step 4: Apply graph updates without replay-state replacement**

In each tab, display active filename/version beside the dataset badge. On a
`graph` event, fetch `/graph`, index nodes by their real ID, enrich entity/room
details, then call:

```javascript
const before = viewer.serializeState();
const outcome = viewer.applyGraph(payload.graph, payload.version);
viewer.restoreState(before);
if (outcome.selectedEntityMissing) viewer.clearSelection('更新后的场景图中已不存在该实体');
```

`restoreState` must not restore a missing selected entity. Insert one visible
status notice per version. Background tabs consume and store graph events but
defer DOM/WebGL application until activation.

- [ ] **Step 5: Add browser hot-reload assertions**

Open a result, move the playhead and camera, select an entity, atomically write
`dsg_updated.json`, wait for its badge, and assert time/camera/selection remain.
Then replace it with a graph without that entity and assert only selection is
cleared. Open another tab and verify its graph badge does not change.

- [ ] **Step 6: Run tests and commit**

Run:

```bash
python3 -m unittest tests.test_replay_events tests.test_replay_artifacts tests.test_replay_web_server -v
RUN_BROWSER_TESTS=1 python -m unittest tests.test_replay_workspace_browser -v
```

Expected: PASS.

```bash
git add CHANGELOG.md scripts/replay_web/events.py scripts/replay_web/server.py scripts/replay_web/static tests/test_replay_events.py tests/test_replay_web_server.py tests/test_replay_workspace_browser.py
git commit -m "feat: hot reload replay scene graphs"
```

---

### Task 3: Give postprocess and summarize atomic stage outputs

**Files:**
- Modify: `scripts/postprocess_scene_graph.py`
- Modify: `scripts/summarize_regions.py`
- Modify: `scripts/postprocess_coda.sh`
- Modify: `tests/test_coda_scripts.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: `postprocess_scene_graph.py --output PATH` with the current default preserved.
- Produces: `summarize_regions.py --summary-output PATH --scene-graph-output PATH` with current defaults preserved.
- Produces: `postprocess_coda.sh --stage postprocess|summarize|all [--atomic] RUN...`.
- Preserves: no `--stage` means postprocess; existing `--summaries` means `all`.

- [ ] **Step 1: Add failing shell tests for stage isolation and old-output safety**

```python
result = self._run("scripts/postprocess_coda.sh", "--stage", "summarize", "out_1", OPENAI_API_KEY="test")
self.assertEqual(result.returncode, 0, result.stderr)
self.assertNotIn("postprocess_scene_graph.py", self._calls())
self.assertIn("summarize_regions.py", self._calls())
```

Add tests that postprocess does not call summary, summarize refuses missing
`clustered_dsg.json`, invalid stage exits 2, existing CLI semantics remain,
failure leaves pre-existing final files byte-identical, successful atomic mode
renames both graph/YAML summary outputs, and no `.replay-job-*` files remain.

- [ ] **Step 2: Verify tests fail against the current monolithic script**

Run: `python3 -m unittest tests.test_coda_scripts.PostprocessCodaTest -v`

Expected: stage-option tests FAIL.

- [ ] **Step 3: Add explicit output paths to Python writers**

For embeddings:

```python
@click.option("--output", type=click.Path(), default=None)
def main(data_dir: str, sentence_model_name: str, output: Optional[str]):
    updated_path = Path(output) if output else Path(data_dir) / "dsg_updated.json"
    scene_graph.save(str(updated_path))
```

For summaries, separate `--summary-output` and `--scene-graph-output`; preserve
`region_summaries.yaml` and `<input>_with_summaries.json` defaults when omitted.
Write YAML through a temporary file and `os.replace` even outside web mode.

- [ ] **Step 4: Refactor the shell script into exact stage functions**

Implement `run_embeddings`, `run_clustering`, and `run_summaries`. In atomic
mode create temp names in the result directory using the current PID:

```bash
tmp_updated="${dir}/.replay-job-${$}-dsg_updated.json"
tmp_clustered="${dir}/.replay-job-${$}-clustered_dsg.json"
tmp_summaries="${dir}/.replay-job-${$}-region_summaries.yaml"
tmp_summary_graph="${dir}/.replay-job-${$}-clustered_dsg_with_summaries.json"
```

Pass explicit input/output paths through the cluster launch for both place
layer variants. Validate every expected temporary output before moving any;
for a multi-file stage, back up existing finals, publish all temps, then restore
backups if any move fails. A trap removes only the exact temp/backups recorded
by this process.

- [ ] **Step 5: Run processing tests and commit**

Run:

```bash
python3 -m unittest tests.test_coda_scripts.PostprocessCodaTest -v
python3 -m compileall -q scripts/postprocess_scene_graph.py scripts/summarize_regions.py
bash -n scripts/postprocess_coda.sh
```

Expected: PASS.

```bash
git add CHANGELOG.md scripts/postprocess_scene_graph.py scripts/summarize_regions.py scripts/postprocess_coda.sh tests/test_coda_scripts.py
git commit -m "fix: publish postprocess outputs atomically"
```

---

### Task 4: Supervise allowlisted processing jobs

**Files:**
- Create: `scripts/replay_web/jobs.py`
- Create: `tests/test_replay_jobs.py`
- Modify: `scripts/replay_web/server.py`
- Modify: `scripts/replay_web/events.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: open run manifest/canonical path, EventBroker, and atomic stages.
- Produces: `JobStage` enum: `postprocess`, `summarize`, `all`.
- Produces: immutable `JobSpec(run_id, result_path, dataset_kind, stage, replace)`.
- Produces: `JobManager.start(spec) -> JobRecord`, `cancel(job_id)`, `get(job_id)`, `list_for_run(run_id)`, `shutdown()`.
- Produces: state sequence `queued -> running -> succeeded|failed|cancelled|interrupted`.
- Produces: `build_job_argv(spec, job_id, repo_root) -> Tuple[List[str], Dict[str, str]]`.

- [ ] **Step 1: Write tests with a fake executable, never real Docker**

```python
manager = JobManager(state_root=state, broker=broker, runner=fake_runner, max_concurrent=1)
first = manager.start(spec(run_id="a"))
with self.assertRaises(JobConflict):
    manager.start(spec(run_id="a", stage=JobStage.SUMMARIZE))
self.assertEqual(manager.get(first.job_id).state, "running")
```

Test global queueing, different-run ordering, explicit replace requirement,
missing prerequisites, TUM/CODa argv/env differences, exact bind mount, no shell,
log bounding, stdout/stderr events, graceful cancel then exact-process
escalation, interrupted recovery from persisted records, shutdown and illegal
state transitions.

- [ ] **Step 2: Verify job tests fail**

Run: `python3 -m unittest tests.test_replay_jobs -v`

Expected: FAIL importing `jobs`.

- [ ] **Step 3: Implement dataset-specific argv without interpolation**

```python
argv = [
    "docker", "compose", "run", "--rm", "-T",
    "--name", f"daaam-replay-job-{job_id}",
    "-v", f"{result_path}:/selected-result",
]
if dataset_kind == "tum":
    argv += ["-e", "CLUSTER_PLACES_CONFIG=config/cluster_places_config_tum.yaml",
             "-e", "PLACES_LAYER=PLACES"]
argv += ["daaam-ws", "bash", "scripts/postprocess_coda.sh",
         "--atomic", "--stage", stage.value, "/selected-result"]
```

Pass this list directly to `subprocess.Popen(..., shell=False,
start_new_session=True, stdout=PIPE, stderr=STDOUT, text=True)`. Persist one
JSON record and one bounded UTF-8 log under `.replay-web/jobs/<job_id>/`.
Cancellation sends SIGTERM to the recorded process group, waits ten seconds,
then invokes `docker stop daaam-replay-job-<id>` as a fixed argv and finally
SIGKILLs only the recorded group if needed.

- [ ] **Step 4: Publish state/log events and recovery**

Emit `job-state` and `job-log` through EventBroker. At startup load job records;
rewrite `queued|running` to `interrupted` with a recovery message. On success,
request an immediate ArtifactMonitor scan for that run. Never infer success
only from output existence; require child exit 0 and expected final artifacts.

- [ ] **Step 5: Run tests and commit**

Run: `python3 -m unittest tests.test_replay_jobs tests.test_replay_events -v`

Expected: PASS.

```bash
git add CHANGELOG.md scripts/replay_web/jobs.py scripts/replay_web/server.py scripts/replay_web/events.py tests/test_replay_jobs.py
git commit -m "feat: supervise replay processing jobs"
```

---

### Task 5: Expose job APIs and the per-tab task panel

**Files:**
- Modify: `scripts/replay_web/server.py`
- Modify: `tests/test_replay_web_server.py`
- Modify: `scripts/replay_web/static/api.js`
- Modify: `scripts/replay_web/static/app.js`
- Modify: `scripts/replay_web/static/replay.css`
- Modify: `replay.html`
- Modify: `tests/test_replay_workspace_browser.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: `POST /api/runs/{run_id}/jobs` body `{stage, replace}`.
- Produces: `GET /api/runs/{run_id}/jobs` and `POST /api/jobs/{job_id}/cancel`.
- Produces: per-tab task controls `Postprocess`, `Summarize`, `全部运行`, `取消`, and `重新运行` confirmation.

- [ ] **Step 1: Write API authorization and conflict tests**

Test CSRF/origin checks, unknown stage, summarize prerequisite, conflict 409,
existing output without `replace`, cancel unknown/completed job, result isolation,
and JSON status/log-tail serialization. Inject a fake JobManager into
`create_server`; server tests must not start Docker.

- [ ] **Step 2: Add failing browser task-panel test**

```python
self.open_result(self.raw_result)
self.assertTrue(self.button("Postprocess").is_enabled())
self.assertFalse(self.button("Summarize").is_enabled())
self.button("Postprocess").click()
self.fake_job.emit_log("=== sentence embeddings")
self.assertIn("sentence embeddings", self.task_log().text)
self.fake_job.succeed_and_write("clustered_dsg.json")
self.wait_for_graph_badge("clustered_dsg.json")
self.assertTrue(self.button("Summarize").is_enabled())
```

Add rerun confirmation, cancellation, background-tab completion badge and one
tab's logs never appearing in another tab.

- [ ] **Step 3: Implement API methods and state-derived controls**

```javascript
async startJob(runId, stage, replace = false) {
  return this.request(`/api/runs/${encodeURIComponent(runId)}/jobs`, {
    method: 'POST', body: {stage, replace}
  });
}

async cancelJob(jobId) {
  return this.request(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {method: 'POST', body: {}});
}
```

Compute availability from active graph/artifact state returned by the server,
not filenames guessed in JavaScript. Stream job events into a bounded 500-line
view. A failed job shows exit status and retry; it must not change the active
graph badge. A background tab stores events and updates only its small tab
status marker until activated.

- [ ] **Step 4: Run server/browser tests and commit**

Run:

```bash
python3 -m unittest tests.test_replay_web_server tests.test_replay_jobs -v
RUN_BROWSER_TESTS=1 python -m unittest tests.test_replay_workspace_browser -v
```

Expected: PASS.

```bash
git add CHANGELOG.md replay.html scripts/replay_web/server.py scripts/replay_web/static tests/test_replay_web_server.py tests/test_replay_workspace_browser.py
git commit -m "feat: run replay processing from the web"
```

---

### Task 6: Verify TUM and CODa processing workflows and document recovery

**Files:**
- Modify: `USAGE.md`
- Modify: `DOCKER.md`
- Modify: `justfile`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces: documented web and CLI processing parity, job-state storage, graph priority and safe rerun behaviour.

- [ ] **Step 1: Add processing suites to `just test`**

Include `tests.test_replay_artifacts`, `tests.test_replay_events`, and
`tests.test_replay_jobs` in the host suite. Keep current CODa/TUM script tests
and browser routing.

- [ ] **Step 2: Document exact operator behaviour**

Document the four-file graph priority, two-second polling, postprocess vs
summarize prerequisites, rerun confirmation, cancellation semantics,
`.replay-web/jobs/`, interrupted recovery, API-key requirement for summaries,
and CLI equivalents. Explain that playback can use raw `dsg.json`, while
summaries and later Agent queries may require derived data.

- [ ] **Step 3: Run all automated tests**

Run: `just test`

Expected: every non-browser suite passes; browser suite passes when enabled or
prints the existing explicit skip.

- [ ] **Step 4: Run TUM processing smoke test through the service**

Use an existing small finalized TUM result, start `just replay-web`, invoke
postprocess in its tab, watch logs, verify promotion to `clustered_dsg.json`,
then invoke summarize only when an API key is configured. Record exact paths and
exit codes. If no key is available, verify the UI prerequisite error without
claiming summary success.

- [ ] **Step 5: Run CODa adapter smoke test without fabricating a result**

Open a real CODa result when available and verify generated argv includes the
CODa defaults; otherwise run the integration fixture that asserts the command
and mark the real-model portion not run. Do not describe fixture output as a
fresh CODa processing result.

- [ ] **Step 6: Check changelog completeness and commit docs**

Run: `git diff --check && git status --short`

Confirm the current-date changelog covers graph monitoring, atomic scripts,
jobs, web controls and documentation.

```bash
git add CHANGELOG.md USAGE.md DOCKER.md justfile
git commit -m "docs: explain replay web processing"
```
