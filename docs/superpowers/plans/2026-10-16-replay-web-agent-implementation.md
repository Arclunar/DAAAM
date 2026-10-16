# Replay Web Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add persistent, isolated, contextual multi-turn scene questions to every replay result tab while keeping model credentials and heavy dependencies in the existing DAAAM worker environment.

**Architecture:** The loopback control service stores provider-neutral conversation JSONL and stages the selected versioned graph into its ignored cache. One long-lived NDJSON worker inside `daaam-ws` owns scene-agent/model caches and emits safe progress/final events; a streaming HTTP endpoint connects that worker to a per-tab chat panel and records the exact model and graph version used for every answer.

**Tech Stack:** Python 3.8+, `spark_dsg`, existing `SceneUnderstandingAgent`, OpenAI/Anthropic provider adapters, JSONL/NDJSON/SSE, Docker Compose, plain JavaScript, unittest, Selenium.

**Spec:** `docs/superpowers/specs/2026-10-16-unified-replay-workspace-design.md`

## Global Constraints

- Execute this plan after the core and processing plans are complete.
- Keep one conversation per canonical result path; switching tabs must never mix messages or graph caches.
- Store history under `.replay-web/history/<canonical-path-hash>/`, never in the selected result.
- Browser code must never receive API keys, hidden prompts, chain-of-thought or unbounded raw tool payloads.
- Agent scene tools remain read-only. Do not expose job methods to the Agent tool registry.
- A question uses one graph version captured at turn start; a later hot reload affects only later turns.
- Preserve chat across service/browser restart and insert a visible version boundary when the active graph changes.
- TUM leaves already-relative scene timestamps unchanged; CODa uses the explicit manifest time origin and optional `background_objects.yaml`.
- The model selector is server allowlisted; default is the current `gpt-5-mini` query model.
- Querying remains available against the last valid graph while processing runs, but it must clearly identify that version.
- Every modification must be reflected accurately in the current-date changelog before commit.

---

### Task 1: Add provider-neutral conversation context and progress hooks to the scene Agent

**Files:**
- Modify: `src/daaam/scene_understanding/services.py`
- Modify: `src/daaam/scene_understanding/providers.py`
- Create: `tests/test_scene_agent_conversation.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: `ConversationTurn = TypedDict("ConversationTurn", {"role": Literal["user", "assistant"], "content": str})`.
- Produces: `ProgressCallback = Callable[[Mapping[str, Any]], None]`.
- Extends: `SceneUnderstandingAgent.answer_query(ResponseFormat, query, image=None, *, conversation=None, context=None, progress=None) -> Tuple[Response, Dict[str, Any], List[Any]]`.
- Preserves: callers without new keywords receive the same three-tuple `(parsed_response, interaction_history, provider_messages)`.

- [ ] **Step 1: Write fake-provider tests before touching production code**

```python
answer, history, messages = agent.answer_query(
    TextResponse,
    "Where is it now?",
    conversation=[
        {"role": "user", "content": "Find the red chair."},
        {"role": "assistant", "content": "It is beside the desk."},
    ],
    context={"replay_time": 12.5, "selected_entity": "O7"},
    progress=events.append,
)
self.assertIn("Find the red chair", fake_client.last_input_text())
self.assertIn("replay_time: 12.5", fake_client.last_input_text())
self.assertEqual([event["type"] for event in events], ["iteration", "tool", "final"])
```

Provide fake OpenAI Responses and Anthropic Messages clients and a fake tool
registry. Test empty history parity, role validation, context length bound,
iteration/tool names without tool result payload, provider-specific message
format, and progress callback exceptions being logged but not failing a query.

- [ ] **Step 2: Run tests and observe ignored history/progress**

Run: `docker compose run --rm -T daaam-ws python -m unittest tests.test_scene_agent_conversation -v`

Expected: FAIL because the new keywords are not consumed and no events fire.

- [ ] **Step 3: Build provider-neutral input messages**

```python
def _conversation_messages(turns: Optional[Sequence[ConversationTurn]]) -> List[Dict[str, Any]]:
    messages = []
    for turn in turns or ():
        if turn["role"] not in ("user", "assistant") or not isinstance(turn["content"], str):
            raise ValueError("conversation turns require user/assistant string content")
        messages.append({"role": turn["role"], "content": turn["content"]})
    return messages

def _context_text(context: Optional[Mapping[str, Any]]) -> str:
    if not context:
        return ""
    allowed = {key: context[key] for key in ("replay_time", "camera", "selected_entity") if key in context}
    text = "\n".join(f"{key}: {value}" for key, value in allowed.items())
    return f"Current replay context (may be omitted by the user):\n{text}\n" if text else ""
```

For OpenAI, insert prior role/content messages between system instructions and
the current user message. For Anthropic, extend
`build_anthropic_initial_messages` to take prior turns and merge adjacent same
roles if necessary. Cap supplied conversation/context at 64 KiB and raise a
clear `ValueError` before a provider call.

- [ ] **Step 4: Emit safe progress at exact boundaries**

Emit only:

```python
{"type": "iteration", "number": iteration + 1, "maximum": self.config.max_iterations}
{"type": "tool", "name": item.name}
{"type": "final"}
```

Do not emit arguments, tool results, reasoning items, prompts or provider SDK
objects. Add the same hook around Anthropic's loop through an optional callback
argument in `run_anthropic_tool_loop`.

- [ ] **Step 5: Run provider tests and commit**

Run:

```bash
docker compose run --rm -T daaam-ws python -m unittest tests.test_scene_agent_conversation -v
docker compose run --rm -T daaam-ws python -m compileall -q src/daaam/scene_understanding
```

Expected: PASS.

```bash
git add CHANGELOG.md src/daaam/scene_understanding/services.py src/daaam/scene_understanding/providers.py tests/test_scene_agent_conversation.py
git commit -m "feat: add conversation context to scene queries"
```

---

### Task 2: Load TUM/CODa query graphs and cache Agent instances

**Files:**
- Create: `scripts/replay_web/query.py`
- Create: `tests/test_replay_query.py`
- Modify: `scripts/demo_query.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: manifest dataset metadata and graph provenance.
- Produces: immutable `DatasetContext(kind, sequence, time_origin, places_layer)`.
- Produces: `load_query_graph(graph_path: Path, dataset: DatasetContext) -> DynamicSceneGraph`.
- Produces: `query_capability(graph_path: Path) -> QueryCapability(ready, reason, provenance)`.
- Produces: `AgentCache(max_graphs=2)` with `get(result_key, graph_path, graph_version, dataset, model_name) -> SceneUnderstandingAgent` and `invalidate(result_key)`.
- Preserves: `demo_query.py` CLI, but allows `--dataset-kind tum|coda` and explicit `--time-origin` so TUM no longer requires a CODa `seq-id`.

- [ ] **Step 1: Write adapter and cache tests**

```python
tum = load_query_graph(graph_path, DatasetContext("tum", "fr1_desk", 0.0, "PLACES"))
self.assertEqual(object_timestamp(tum), original_relative_timestamp)
coda = load_query_graph(graph_path, DatasetContext("coda", "0", START_TIMES[0], "TRAVERSABILITY"))
self.assertEqual(object_timestamp(coda), original_absolute_timestamp - START_TIMES[0])
```

Add tests for optional background enrichment, missing sentence provenance
yielding `ready=False`, CLIP backend/model selected from stored provenance,
same key reusing an Agent, graph version change rebuilding it, LRU eviction,
model change keeping graph but constructing a provider-specific Agent, and
cache invalidation by result key.

- [ ] **Step 2: Run in the DAAAM container and observe missing module**

Run: `docker compose run --rm -T daaam-ws python -m unittest tests.test_replay_query -v`

Expected: FAIL importing `scripts.replay_web.query`.

- [ ] **Step 3: Extract query setup from the CLI**

```python
def load_query_graph(graph_path: Path, dataset: DatasetContext):
    graph = sdsg.DynamicSceneGraph.load(str(graph_path))
    if dataset.kind == "coda":
        background = graph_path.parent / "background_objects.yaml"
        graph = preprocess_scene_graph(graph, dataset.time_origin, background if background.exists() else None)
    elif dataset.kind != "tum":
        raise QueryConfigurationError(f"unsupported dataset kind: {dataset.kind}")
    return graph
```

Build Agent config with exactly the existing four read-only tools. Resolve
`EmbeddingProvenance` before constructing/reusing CLIP and sentence handlers.
Never infer TUM/CODa or sequence from a directory name.

- [ ] **Step 4: Reuse the helper from `demo_query.py`**

Keep old CODa invocations valid: `--seq-id` supplies kind `coda` and the current
`START_TIMES` lookup when new flags are absent. Add an explicit TUM example to
`--help`. The CLI remains a single-turn REPL and need not persist history.

- [ ] **Step 5: Run query and CLI tests and commit**

Run:

```bash
docker compose run --rm -T daaam-ws python -m unittest tests.test_replay_query -v
docker compose run --rm -T daaam-ws python scripts/demo_query.py --help
```

Expected: tests PASS and help documents both datasets.

```bash
git add CHANGELOG.md scripts/replay_web/query.py scripts/demo_query.py tests/test_replay_query.py
git commit -m "feat: load query graphs for tum and coda"
```

---

### Task 3: Persist isolated conversation histories and graph boundaries

**Files:**
- Create: `scripts/replay_web/history.py`
- Create: `tests/test_replay_history.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: `history_key(canonical_path: Path) -> str` as SHA-256 of the canonical UTF-8 path.
- Produces: immutable `ChatMessage(message_id, role, content, created_at, model, graph_name, graph_version, context)` where role is `user`, `assistant`, `boundary`, or `error`.
- Produces: `ConversationStore(root, canonical_path)` with `messages()`, `append_user(...)`, `append_assistant(...)`, `append_boundary(...)`, `append_error(...)`, `context_messages(max_chars=48000)`, and `clear()`.
- Produces: metadata `schema_version`, `canonical_path`, `created_at`, and optional compacted summary.

- [ ] **Step 1: Write persistence, isolation and compaction tests**

```python
first = ConversationStore(root, Path("/data/a"))
second = ConversationStore(root, Path("/data/b"))
first.append_user("Where is the chair?", model="gpt-5-mini", graph=version)
first.append_assistant("Beside the desk.", model="gpt-5-mini", graph=version)
self.assertEqual(len(ConversationStore(root, Path("/data/a")).messages()), 2)
self.assertEqual(second.messages(), [])
```

Test canonical equivalent paths share a key, clear affects one result only,
malformed trailing JSONL line is quarantined without losing earlier turns,
graph boundary de-duplicates consecutive identical versions, append/fsync,
metadata never contains API keys, and long history produces one bounded
extractive summary plus the newest full turns.

- [ ] **Step 2: Verify tests fail**

Run: `python3 -m unittest tests.test_replay_history -v`

Expected: FAIL importing `history`.

- [ ] **Step 3: Implement append-only JSONL and safe clearing**

```python
def history_key(canonical_path: Path) -> str:
    return hashlib.sha256(str(canonical_path.resolve()).encode("utf-8")).hexdigest()

def _append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
```

Validate roles and maximum single-message size (64 KiB). `clear()` atomically
renames the exact history directory to a trash sibling, creates a fresh store,
then removes only that sibling. Never recursively delete a computed path before
verifying it is a direct child of the configured history root.

- [ ] **Step 4: Implement deterministic bounded context**

Walk newest user/assistant messages backward until 32,000 characters are retained; boundary and error rows stay visible in stored history but are excluded from provider context. Summarize
older user/assistant pairs into lines no longer than 400 characters each under
`Earlier conversation summary:` and cap the combined context at 48,000
characters. Persist the summary metadata only after successful generation; no
extra provider call is made. Preserve graph filenames/versions in the summary
line so old answers remain attributable.

- [ ] **Step 5: Run tests and commit**

Run: `python3 -m unittest tests.test_replay_history -v`

Expected: PASS.

```bash
git add CHANGELOG.md scripts/replay_web/history.py tests/test_replay_history.py
git commit -m "feat: persist replay conversations per result"
```

---

### Task 4: Run one long-lived Agent worker through an NDJSON protocol

**Files:**
- Create: `scripts/replay_web/agent_protocol.py`
- Create: `scripts/replay_web/agent_worker.py`
- Create: `scripts/replay_web/agent_client.py`
- Create: `tests/test_replay_agent_protocol.py`
- Create: `tests/test_replay_agent_client.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: query loader/cache from Task 2 and provider-neutral turns from Task 1.
- Produces: `inspect` request `{id, action:"inspect", result_key, graph_path, graph_version, dataset}` and `ask` request `{id, action:"ask", result_key, graph_path, graph_version, dataset, model, question, conversation, context}`.
- Produces: events `{id, type:"progress"|"delta"|"complete"|"error", ...}`.
- Produces: `AgentClient.start()`, `inspect(request)`, `ask(request, on_event)`, `invalidate(result_key)`, and `stop()`.
- Produces: one shared worker process by default and a request queue; no per-tab model process.

- [ ] **Step 1: Test protocol validation and a fake worker process**

```python
request = parse_request({
    "id": "turn-1", "action": "ask", "result_key": "run-hash", "graph_path": "cache/run/v.json",
    "graph_version": "sha256", "dataset": {"kind": "tum", "sequence": "fr1_desk", "time_origin": 0.0},
    "model": "gpt-5-mini", "question": "Where is the chair?", "conversation": [], "context": {},
})
self.assertEqual(request.dataset.kind, "tum")
```

Reject absolute/parent graph paths, unknown actions/models, oversized questions
and invalid roles. Fake-worker tests cover progress/delta/complete ordering,
stderr isolation, two queued requests, one request error not killing the
worker, crash restart once, timeout, shutdown, and no API key in serialized
events. Give the fake response a distinctive private `reasoning` string and
assert it is absent from every worker event and stored history record.

- [ ] **Step 2: Run tests and confirm modules are absent**

Run: `python3 -m unittest tests.test_replay_agent_protocol tests.test_replay_agent_client -v`

Expected: FAIL importing the new modules.

- [ ] **Step 3: Implement a stdout-clean worker**

```python
def emit(value: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    request = parse_request(json.loads(line))
    try:
        with contextlib.redirect_stdout(sys.stderr):
            answer = handle_ask(request, cache, progress=lambda event: emit(progress_event(request.id, event)))
        for chunk in text_chunks(answer.answer, 80):
            emit({"id": request.id, "type": "delta", "text": chunk})
        emit({"id": request.id, "type": "complete", "answer": answer.answer})
    except Exception as error:
        emit({"id": request.id, "type": "error", "message": public_error(error)})
```

Only paths relative to the repository `.replay-web/cache/` are accepted by the
worker. The control service stages a graph with atomic copy under
`.replay-web/cache/<history-key>/<graph-digest>.json`; the container sees the
same repository-relative path through the existing Compose mount.

For `inspect`, call `query_capability` and emit one `complete` event containing
only `{ready, reason, provenance}`; do not construct an LLM provider client.
For `invalidate`, evict matching graph/Agent cache entries and acknowledge with
an empty `complete`. These actions use the same validated relative paths and
request IDs as `ask`.

- [ ] **Step 4: Implement the host-side process client**

Start fixed argv:

```python
[
    "docker", "compose", "run", "--rm", "-T", "--name", "daaam-replay-agent",
    "-e", "HF_HOME=/data/hf_cache", "daaam-ws",
    "python", "-m", "scripts.replay_web.agent_worker",
]
```

Use one writer lock, one stdout reader thread, one stderr logger thread and a
pending-request map. Default concurrency is one; later requests queue rather
than starting another container. Redact environment/keys from logs. On clean
service shutdown send EOF, wait ten seconds, then stop only the fixed named
container if still alive.

- [ ] **Step 5: Run protocol/client tests and commit**

Run:

```bash
python3 -m unittest tests.test_replay_agent_protocol tests.test_replay_agent_client -v
python3 -m compileall -q scripts/replay_web
```

Expected: PASS without Docker because `AgentClient` receives an injected argv/process factory.

```bash
git add CHANGELOG.md scripts/replay_web/agent_protocol.py scripts/replay_web/agent_worker.py scripts/replay_web/agent_client.py tests/test_replay_agent_protocol.py tests/test_replay_agent_client.py
git commit -m "feat: add a shared replay agent worker"
```

---

### Task 5: Stream per-tab chat through the service and browser

**Files:**
- Modify: `scripts/replay_web/server.py`
- Modify: `scripts/replay_web/runs.py`
- Modify: `tests/test_replay_web_server.py`
- Modify: `scripts/replay_web/static/api.js`
- Modify: `scripts/replay_web/static/app.js`
- Modify: `scripts/replay_web/static/replay.css`
- Modify: `replay.html`
- Modify: `tests/test_replay_workspace_browser.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: active graph version, `ConversationStore`, and `AgentClient`.
- Produces: `GET /api/runs/{run_id}/chat`, streaming `POST /api/runs/{run_id}/chat`, and `DELETE /api/runs/{run_id}/chat`.
- Extends: `GET /api/config` with `agent_models`, `default_agent_model`, and query availability without credentials.
- Produces: request body `{question, model, include_view_context, view_context}`.
- Produces: SSE events `progress`, `delta`, `complete`, and `error`, each carrying turn ID; `complete` includes model and graph provenance.

- [ ] **Step 1: Write service tests with a fake AgentClient**

Test allowed/disallowed model, missing query-ready graph, API credential status
without returning key values, active version captured before a concurrent graph
event, persisted user/assistant turns only after success, retry after failure,
history restoration, clear-one-result isolation, context opt-out, question size,
CSRF/origin and worker-unavailable 503.

```python
events = post_sse(base + f"/api/runs/{run_id}/chat", {
    "question": "Where is the chair?", "model": "gpt-5-mini",
    "include_view_context": True,
    "view_context": {"replay_time": 12.5, "selected_entity": "O7", "camera": "rgb"},
})
self.assertEqual(events[-1]["event"], "complete")
self.assertEqual(events[-1]["data"]["graph"]["name"], "dsg_updated.json")
```

- [ ] **Step 2: Add failing browser multi-turn tests**

Open two tabs. In the first, ask a question and follow-up, assert the fake worker
receives the first answer in `conversation`; in the second, assert empty
conversation. Reload and verify messages return. Promote the first graph,
assert a visible boundary, ask again and assert the new graph version. Clear the
first and assert the second remains. Toggle current-view context and inspect the
fake request.

- [ ] **Step 3: Implement streaming route and exact persistence order**

At turn start:

1. validate model/question/context;
2. capture active validated graph/version;
3. stage that exact graph by digest;
4. load bounded prior conversation;
5. append the user message with captured provenance;
6. stream worker events;
7. append only the public answer text as assistant content on `complete`;
8. append a retryable error record on failure without inventing an answer or persisting model reasoning.

Set `Content-Type: text/event-stream`, `Cache-Control: no-store`, and flush every
event. If the browser disconnects, let the bounded worker request finish and
persist its answer; a restored chat can then show it.

When ArtifactMonitor activates a graph, stage it and call
`AgentClient.inspect`; cache that lightweight capability beside the active
version in the open-run record and publish it in the next graph/state event.
The WSL control service must not import `spark_dsg` or model libraries. A worker
startup/inspect failure marks query unavailable while leaving replay and jobs
fully usable.

- [ ] **Step 4: Implement the per-tab chat panel**

The panel contains model selector, “包含当前视图上下文” checked by default,
message list, tool-progress line, textarea, send/cancel-display button and
`清空对话`. Render messages with `textContent`, never `innerHTML`. For each
assistant turn show model, graph filename and short digest. Render progress as
`第 N 轮` or `查询场景: TOOL_NAME`; do not show raw arguments/results.

On graph event, insert one boundary row only when version differs from the last
chat provenance. Disable send with an actionable `请先运行 postprocess` reason
when `query_capability.ready` is false. A processing job may run while send
remains enabled against the displayed old version.

- [ ] **Step 5: Run service and browser tests**

Run:

```bash
python3 -m unittest tests.test_replay_history tests.test_replay_agent_protocol tests.test_replay_agent_client tests.test_replay_web_server -v
RUN_BROWSER_TESTS=1 python -m unittest tests.test_replay_workspace_browser -v
```

Expected: PASS.

- [ ] **Step 6: Record and commit web chat**

```bash
git add CHANGELOG.md replay.html scripts/replay_web/server.py scripts/replay_web/runs.py scripts/replay_web/static tests/test_replay_web_server.py tests/test_replay_workspace_browser.py
git commit -m "feat: ask contextual scene questions in replay"
```

---

### Task 6: Verify persistent multi-turn Agent behaviour and document operations

**Files:**
- Modify: `USAGE.md`
- Modify: `DOCKER.md`
- Modify: `justfile`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces: documented model allowlist, credentials, per-result history, graph provenance, worker lifecycle and recovery.

- [ ] **Step 1: Add Agent suites to `just test`**

Add host protocol/history/client tests and container conversation/query tests.
Keep the optional browser run and existing tests. The recipe must not make a
real provider request.

- [ ] **Step 2: Document configuration and privacy boundaries**

Document `REPLAY_AGENT_MODELS` as a comma-separated allowlist with default
`gpt-5-mini`, required provider environment variables, `.replay-web/history/`,
the shared worker/container name, context toggle, graph/model provenance,
clearing one conversation, and how to stop/restart the service safely. State
that job controls are not Agent tools and that browser clients never receive
keys.

- [ ] **Step 3: Run the complete regression suite**

Run: `just test`

Expected: all suites PASS; optional browser tests pass or print only the
documented Selenium skip.

- [ ] **Step 4: Run a real TUM multi-turn smoke test**

Use a real TUM result with `dsg_updated.json` or better, start
`just replay-web`, ask an object/location question and a follow-up pronoun
question, reload the browser and verify both turns persist. Record the model,
result path, graph filename/digest and exact success/failure; never present a
fixture response as a model result.

- [ ] **Step 5: Run CODa only when a real query-ready result and credentials exist**

Open a real CODa result, verify manifest time origin and one query. If either
data or credentials are missing, report that specific unrun condition and rely
only on the adapter/worker integration tests; do not claim a fresh CODa answer.

- [ ] **Step 6: Verify graph reload provenance**

With one chat open, atomically publish a higher-priority valid graph, wait for
the boundary, ask another question and verify the new answer metadata names the
new version while the older answer retains its old provenance.

- [ ] **Step 7: Audit changelog, diff and secrets before commit**

Run:

```bash
git diff --check
git status --short
rg -n "OPENAI_API_KEY|ANTHROPIC_API_KEY" replay.html scripts/replay_web/static .replay-web/history 2>/dev/null
```

Expected: no key values in web assets/history, and current-date changelog
entries cover Agent context, worker, persistence, UI and docs.

- [ ] **Step 8: Commit docs and test routing**

```bash
git add CHANGELOG.md USAGE.md DOCKER.md justfile
git commit -m "docs: explain replay agent conversations"
```
