# Unified Replay Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-result embedded replay pages with one loopback-served workspace application that opens finalized TUM or CODa replay bundles in isolated tabs.

**Architecture:** `scripts/replay.py` keeps recording and Rerun export, while a focused finalizer writes a versioned `replay/` directory containing a manifest, gzip timeline chunks and image assets. A standard-library WSL HTTP service validates arbitrary WSL-visible result paths and serves opaque run IDs to one root `replay.html`; the existing WebGL viewer becomes a reusable per-tab component backed by an HTTP chunk data source.

**Tech Stack:** Python 3.8+, NumPy, OpenCV, standard-library `http.server`/`gzip`/`json`, plain ES2020 JavaScript, WebGL 1, Selenium browser tests, unittest, Docker Compose, just.

**Spec:** `docs/superpowers/specs/2026-10-16-unified-replay-workspace-design.md`

## Global Constraints

- Keep exactly one HTML entry point at repository root: `replay.html`; never generate HTML inside a result.
- Do not support legacy embedded replay HTML. A result must contain a valid `replay/manifest.json`.
- Support both `tum` and `coda` through explicit manifest metadata, never directory-name inference.
- Default discovery root is repository `output/`; an entered path may be any absolute directory visible from WSL.
- Accept Linux paths, `/mnt/<drive>/...`, Windows drive-letter paths, and the current distro's `\\wsl.localhost\...` path.
- Bind the service to `127.0.0.1`; do not add remote serving or authentication in this phase.
- Multiple results use tabs. Only the active tab renders/decodes; there is no side-by-side comparison.
- Object/Place selection and the description-frame modal must never move the timeline.
- Keep bare `just`, existing postprocess/summarize recipes, and optional `replay.rrd` behaviour intact.
- Use TDD and make every task an independently reviewable commit.
- Every repository modification, including tests and docs, must be represented accurately under `2026-10-16` in `CHANGELOG.md` before that task is committed.

---

### Task 1: Define and validate the replay bundle manifest

**Files:**
- Create: `scripts/__init__.py`
- Create: `scripts/replay_web/__init__.py`
- Create: `scripts/replay_web/manifest.py`
- Create: `tests/test_replay_manifest.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: `SCHEMA_VERSION: int = 1`.
- Produces: `ManifestError(ValueError)`.
- Produces: `validate_manifest(bundle_root: Path) -> Dict[str, Any]`.
- Produces: `resolve_bundle_member(bundle_root: Path, relative: str) -> Path`.
- Produces: `write_json_atomic(path: Path, value: Mapping[str, Any]) -> None`.
- Consumes: no earlier task.

- [ ] **Step 1: Write failing manifest tests**

```python
class ReplayManifestTest(unittest.TestCase):
    def test_valid_manifest_resolves_only_members_inside_the_bundle(self):
        root = self.make_bundle(dataset_kind="tum")
        manifest = validate_manifest(root)
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(resolve_bundle_member(root, "timeline/000000.json.gz"), root / "timeline/000000.json.gz")

    def test_absolute_parent_and_escaping_symlink_members_are_rejected(self):
        root = self.make_bundle(dataset_kind="coda")
        for value in ("/etc/passwd", "../outside", "timeline/../../outside"):
            with self.subTest(value=value), self.assertRaises(ManifestError):
                resolve_bundle_member(root, value)
```

The fixture manifest must contain `schema_version`, `finalized`, `dataset`,
`timebase`, `snapshots`, `chunks`, `cameras`, `images`, and `graph_candidates`.
Add separate failures for an unknown schema, `finalized: false`, a dataset kind
outside `tum|coda`, a missing chunk, a bad SHA-256 string and a chunk whose
resolved symlink leaves `bundle_root`.

- [ ] **Step 2: Run the tests and verify the module is missing**

Run: `python3 -m unittest tests.test_replay_manifest -v`

Expected: FAIL importing `scripts.replay_web.manifest`.

- [ ] **Step 3: Implement the exact schema boundary**

```python
SCHEMA_VERSION = 1
DATASET_KINDS = frozenset({"tum", "coda"})
GRAPH_CANDIDATES = (
    "clustered_dsg_with_summaries.json",
    "clustered_dsg.json",
    "dsg_updated.json",
    "dsg.json",
)

class ManifestError(ValueError):
    pass

def resolve_bundle_member(bundle_root: Path, relative: str) -> Path:
    member = Path(relative)
    if member.is_absolute() or ".." in member.parts:
        raise ManifestError(f"unsafe bundle member: {relative}")
    root = bundle_root.resolve(strict=True)
    resolved = (root / member).resolve(strict=True)
    if resolved != root and root not in resolved.parents:
        raise ManifestError(f"bundle member leaves {root}: {relative}")
    return resolved
```

Validate required keys and types explicitly; do not silently fill missing
values. Validate every chunk/image/reference member through
`resolve_bundle_member`. Implement `write_json_atomic` with a sibling named
`.NAME.PID.tmp`, `flush`, `os.fsync`, and `os.replace`, deleting the temporary
file in `finally`.

- [ ] **Step 4: Run focused tests and formatting checks**

Run: `python3 -m unittest tests.test_replay_manifest -v`

Expected: all manifest tests PASS.

Run: `python3 -m compileall -q scripts/replay_web tests/test_replay_manifest.py && git diff --check`

Expected: exit 0.

- [ ] **Step 5: Record and commit the contract**

Add one concise `CHANGELOG.md` bullet describing the versioned manifest and
path-traversal boundary, then commit only this task:

```bash
git add CHANGELOG.md scripts/__init__.py scripts/replay_web/__init__.py scripts/replay_web/manifest.py tests/test_replay_manifest.py
git commit -m "feat: define replay bundle manifest"
```

---

### Task 2: Finalize recordings into chunked replay bundles

**Files:**
- Create: `scripts/replay_bundle.py`
- Create: `scripts/replay_datasets.py`
- Create: `tests/test_replay_bundle.py`
- Create: `tests/test_replay_datasets.py`
- Modify: `scripts/replay.py`
- Modify: `tests/test_replay_catalog.py`
- Modify: `tests/test_replay_reference_mesh.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `validate_manifest`, `write_json_atomic`, `SCHEMA_VERSION`, and `GRAPH_CANDIDATES` from Task 1.
- Consumes: snapshot/catalog/reference helpers already present in `scripts.replay.py`.
- Produces: `DatasetAssets` protocol plus `TumDatasetAssets` and `CodaDatasetAssets`, selected only by explicit dataset kind.
- Produces: immutable `BundleOptions(recording, source_graph, run_dir, output, dataset_kind, sequence, dataset, fps, time_origin, start_frame, frame_count, title, merge, thumb_width, max_depth, reference_mesh, max_chunk_snapshots, max_chunk_bytes)`.
- Produces: `finalize_bundle(options: BundleOptions) -> Path`, returning the validated `replay/manifest.json` path.
- Produces: `python scripts/replay.py finalize RECORDING --run RUN --dataset-kind tum|coda --sequence NAME --out RUN/replay ...`.

- [ ] **Step 1: Write a failing small-bundle test**

```python
def test_finalize_writes_manifest_last_and_external_image_assets(self):
    options = self.options(dataset_kind="tum", sequence="fr1_desk", max_chunk_snapshots=2)
    manifest_path = finalize_bundle(options)
    manifest = json.loads(manifest_path.read_text())
    self.assertEqual(manifest["dataset"]["kind"], "tum")
    self.assertEqual(manifest["dataset"]["sequence"], "fr1_desk")
    self.assertEqual(manifest["dataset"]["time_origin"], 0.0)
    self.assertEqual([c["snapshot_count"] for c in manifest["chunks"]], [2, 1])
    self.assertFalse(any("data:image" in p.read_text(errors="ignore") for p in options.output.rglob("*.json")))
    self.assertTrue((options.output / manifest["images"]["timeline"][0]["rgb"]).is_file())
    self.assertEqual(validate_manifest(options.output)["snapshots"]["count"], 3)
```

Use three tiny `.npz` snapshots, a two-frame RGB/depth dataset,
`corrections.yaml` with one exact `description_source`, and the existing tiny
reference-mesh fixture. Add failures for an empty recording and unsupported
dataset kind. Add a size-bound test in which two payloads exceed
`max_chunk_bytes` and therefore land in separate chunks.

Add a CODa fixture with deliberately lexicographically misleading names
`..._1.png`, `..._2.png`, and `..._10.png` under
`2d_rect/cam0/0/` and matching `3d_raw_estimated/cam0/0/`. Assert numeric frame
ordering, `time_origin` read from `timestamps/0.txt`, `start_frame` offset for a
run-relative description frame, and a one-snapshot bundle created directly
from a final `dsg.json` when no historical recording exists.

- [ ] **Step 2: Verify the finalizer test fails**

Run: `docker compose run --rm -T daaam-ws python -m unittest tests.test_replay_bundle -v`

Expected: FAIL because `scripts.replay_bundle` and the `finalize` command do not exist.

- [ ] **Step 3: Implement bundle encoding and atomic publication**

Use this public dataclass and chunk shape:

```python
@dataclass(frozen=True)
class BundleOptions:
    recording: Optional[Path]
    source_graph: Optional[Path]
    run_dir: Path
    output: Path
    dataset_kind: str
    sequence: str
    dataset: Optional[Path] = None
    fps: float = 15.0
    time_origin: float = 0.0
    start_frame: int = 0
    frame_count: Optional[int] = None
    title: str = ""
    merge: float = 0.005
    thumb_width: int = 320
    max_depth: float = 4.0
    reference_mesh: Optional[Path] = None
    max_chunk_snapshots: int = 24
    max_chunk_bytes: int = 8 * 1024 * 1024

def encode_chunk(records: Sequence[Dict[str, Any]]) -> bytes:
    body = json.dumps({"snapshots": list(records)}, separators=(",", ":")).encode("utf-8")
    return gzip.compress(body, compresslevel=6, mtime=0)
```

Each snapshot record contains `n`, rounded `t`, and base64 of the existing
deflate-packed array payload. Partition before writing the next record when
either limit would be exceeded, but always allow one oversized snapshot in its
own chunk. Write `timeline/NNNNNN.json.gz`, calculate SHA-256 and record first
and last time, first snapshot index, count, compressed bytes and uncompressed
payload bytes.

Require exactly one of `recording` or `source_graph`. For `source_graph`, require
positive `frame_count`, load
the final DSG with `spark_dsg`, call the existing `extract_snapshot`, assign one
snapshot number `start_frame + frame_count` and timestamp
`time_origin + (start_frame + frame_count - 1) / fps` from explicit run metadata, and pass it through
the identical encoder. This is the default CODa path because its current ROS
pipeline saves a final graph but does not retain historical ZMQ snapshots; the
bundle protocol and viewer remain identical.

Write RGB, colorized-depth and description-source JPEG bytes under deterministic
names in `images/timeline/` and `images/descriptions/`; manifest indexes carry
relative paths rather than base64. Write the optional reference payload under
`reference/reference.json.gz`. Build everything in `.replay.PID.tmp`, validate
it, rename the old bundle to `.replay.PID.old`, atomically rename the temporary
directory to `replay`, then remove the old directory only after success. Restore
the old directory on publication failure.

Put dataset layout rules in `replay_datasets.py`. TUM uses `rgb/`, `depth/` and
one-based run frame IDs. CODa uses numeric indices from
`2d_rect/cam0/<sequence>/`, `3d_raw_estimated/cam0/<sequence>/`, and
`timestamps/<sequence>.txt`; a run-relative frame ID maps to
`start_frame + frame_id - 1`. Timeline image lookup uses
`round((snapshot_timestamp - time_origin) * fps)` and validates that it lies in
the declared run range. Never sort CODa filenames lexicographically.

- [ ] **Step 4: Add the `finalize` CLI without removing `build` yet**

```python
p = sub.add_parser("finalize", help="turn a recording into a versioned replay data bundle")
p.add_argument("recording", nargs="?")
p.add_argument("--graph")
p.add_argument("--run", required=True)
p.add_argument("--out", required=True)
p.add_argument("--dataset-kind", choices=("tum", "coda"), required=True)
p.add_argument("--sequence", required=True)
p.add_argument("--dataset")
p.add_argument("--fps", type=float, default=15.0)
p.add_argument("--time-origin", type=float, default=0.0)
p.add_argument("--start-frame", type=int, default=0)
p.add_argument("--frame-count", type=int)
p.add_argument("--reference-mesh")
p.set_defaults(func=finalize)
```

Reject anything other than exactly one of positional `recording` or `--graph`.
Keep `record`, `rrd`, and the existing helper APIs stable so their current tests
continue to pass. Update reference/catalog tests to assert bundle metadata and
files in addition to the helper results; do not delete browser/template tests
in this task.

- [ ] **Step 5: Run bundle and existing Python replay tests**

Run:

```bash
docker compose run --rm -T daaam-ws python -m unittest \
  tests.test_replay_manifest tests.test_replay_bundle tests.test_replay_datasets tests.test_replay_catalog \
  tests.test_replay_reference_mesh tests.test_replay_rrd -v
```

Expected: all tests PASS, with no `replay.html` assertion changed yet.

- [ ] **Step 6: Record and commit finalization**

Update the current-date changelog with chunking, external image assets and
atomic bundle publication, then:

```bash
git add CHANGELOG.md scripts/replay.py scripts/replay_bundle.py scripts/replay_datasets.py tests/test_replay_bundle.py tests/test_replay_datasets.py tests/test_replay_catalog.py tests/test_replay_reference_mesh.py
git commit -m "feat: finalize replay data bundles"
```

---

### Task 3: Switch run production from per-result HTML to bundles

**Files:**
- Modify: `scripts/run_replay.sh`
- Modify: `scripts/make_coda_bag.sh`
- Modify: `scripts/run_coda_example.sh`
- Modify: `scripts/replay.py`
- Modify: `tests/test_run_replay.py`
- Modify: `tests/test_coda_scripts.py`
- Modify: `tests/test_justfile.py`
- Modify: `justfile`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `python scripts/replay.py finalize ...` from Task 2.
- Produces: every successful TUM replay run has `RUN/replay/manifest.json` and optional `RUN/replay.rrd`, never `RUN/replay.html`.
- Produces: every newly recorded CODa bag has `daaam_replay.json`, and every successful CODa run has a one-snapshot `RUN/replay/manifest.json` generated from its final `dsg.json`.
- Produces: explicit dataset kind, sequence, time origin and start frame passed to finalization for both workflows.

- [ ] **Step 1: Rewrite shell-policy tests first**

Change the fake Python logger assertions to require one `replay.py finalize`
call with:

```python
self.assertIn("--dataset-kind tum", finalize)
self.assertIn("--sequence fr1_desk", finalize)
self.assertIn("--out output/test_run/out_new/replay", finalize)
self.assertFalse(any(self.root.glob("output/**/replay.html")))
self.assertTrue((self.root / "output/test_run/out_new/replay/manifest.json").is_file())
```

Preserve tests for reference mesh policy, recorder failure, raw-snapshot
retention, Rerun generation and refusing to attach artifacts to an old run.
Update the fake `python` program so `finalize` creates
`$out/manifest.json` and `rrd` creates its file.

For CODa, extend the fake bag/run fixtures first. Assert `make_coda_bag.sh`
writes this sidecar only after a complete bag closes:

```json
{"dataset_kind":"coda","sequence":"0","start_frame":0,"frame_count":1000,"fps":10.0,"time_origin":1673884185.589118}
```

Assert `run_coda_example.sh` finds the new `output/coda/out_*` produced by its
own invocation, reads the sidecar rather than the bag name, invokes `finalize
--graph RUN/dsg.json --dataset-kind coda --sequence 0 --dataset
$CODA_ROOT_DIR --fps 10 --time-origin ... --start-frame ... --frame-count ...`, and never attaches
a bundle to an older output. Missing sidecar must leave the mapping result
intact, return a non-zero artifact status and print the exact command needed to
record a new supported bag.

- [ ] **Step 2: Run tests and observe old `build` behaviour**

Run: `python3 -m unittest tests.test_run_replay tests.test_justfile -v`

Expected: FAIL because the script still invokes `build` and searches for HTML.

- [ ] **Step 3: Change the run workflow and user-facing messages**

Replace the build command with:

```bash
python scripts/replay.py finalize "${RECORDING}" \
  --run "${run_dir}" --dataset-kind tum --sequence "${SEQ}" \
  --title "${NAME} / $(basename "${run_dir}")" \
  "${dataset_args[@]}" "${reference_args[@]}" --out "${run_dir}/replay"
```

Keep Rerun generation as a second command. Print the result directory and
`just replay-web`, not a file URL. In the `tum-run` recipe, remove marker/page
searching and print the newest new result plus its `.rrd` when present. Update
comments from “生成 replay.html” to “生成 replay/ 数据包”.

After tests pass, remove the `build` subcommand and `TEMPLATE` dependency from
`scripts/replay.py`; bundle helpers retained by `replay_bundle.py` must remain
importable. Do not delete `scripts/replay_viewer.html` until Task 7 ports it.

In `make_coda_bag.sh`, read the sequence's first timestamp from
`$CODA_ROOT_DIR/timestamps/0.txt`, validate the requested range, and publish the
sidecar through a temporary file plus `mv` only after the bag's existing
completion check passes. In `run_coda_example.sh`, create a run marker before
launch, resolve only a newer result after launch, and finalize the saved graph
with the sidecar metadata. The static CODa bundle intentionally contains one
time point; adding a historical CODa recorder is not required for the central
workspace, processing or Agent features.

- [ ] **Step 4: Run the shell and recipe suites**

Run:

```bash
python3 -m unittest tests.test_run_replay tests.test_coda_scripts tests.test_justfile -v
just --dry-run tum-run 30 fr1_room
```

Expected: PASS; dry run mentions `run_replay.sh` and does not search for
`replay.html`.

- [ ] **Step 5: Record and commit the no-HTML producer**

Add a changelog bullet stating the intentional incompatibility and new
`replay/` output, then:

```bash
git add CHANGELOG.md justfile scripts/run_replay.sh scripts/make_coda_bag.sh scripts/run_coda_example.sh scripts/replay.py tests/test_run_replay.py tests/test_coda_scripts.py tests/test_justfile.py
git commit -m "refactor: publish replay bundles instead of html"
```

---

### Task 4: Normalize arbitrary result paths and register open runs

**Files:**
- Create: `scripts/replay_web/paths.py`
- Create: `scripts/replay_web/runs.py`
- Create: `tests/test_replay_web_paths.py`
- Create: `tests/test_replay_web_runs.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `validate_manifest(bundle_root)` from Task 1.
- Produces: `normalize_user_path(raw: str, *, distro: str) -> Path`.
- Produces: immutable `OpenRun(run_id, canonical_path, bundle_root, manifest)`.
- Produces: `RunRegistry.open(raw_path: str) -> OpenRun`, `close(run_id)`, `get(run_id)`, and `discover() -> List[OpenRunSummary]`.
- Produces: `run_id` as a random URL-safe opaque token; it must not encode the path.

- [ ] **Step 1: Write path conversion and registry isolation tests**

```python
def test_windows_and_wsl_unc_paths_normalize_to_linux(self):
    self.assertEqual(normalize_user_path(r"E:\daaam-data\run", distro="Ubuntu-20.04"), Path("/mnt/e/daaam-data/run"))
    self.assertEqual(normalize_user_path(r"\\wsl.localhost\Ubuntu-20.04\home\me\run", distro="Ubuntu-20.04"), Path("/home/me/run"))

def test_open_returns_an_opaque_stable_id_for_the_same_canonical_path(self):
    first = registry.open(str(result))
    second = registry.open(str(result / "."))
    self.assertEqual(first.run_id, second.run_id)
    self.assertNotIn(str(result), first.run_id)
```

Also test wrong-distribution UNC rejection, relative-path rejection, missing
manifest, duplicate discoveries, bounded recursive scan depth, close without
deleting files, and two directories with the same basename receiving distinct
IDs.

- [ ] **Step 2: Confirm failures**

Run: `python3 -m unittest tests.test_replay_web_paths tests.test_replay_web_runs -v`

Expected: FAIL importing the new modules.

- [ ] **Step 3: Implement normalization and the thread-safe registry**

```python
WINDOWS_DRIVE = re.compile(r"^(?P<drive>[A-Za-z]):[\\/](?P<tail>.*)$")

def normalize_user_path(raw: str, *, distro: str) -> Path:
    value = raw.strip()
    if match := WINDOWS_DRIVE.match(value):
        return Path("/mnt") / match.group("drive").lower() / Path(match.group("tail").replace("\\", "/"))
    # Parse \\wsl.localhost\DISTRO\... and require DISTRO == distro.
    path = Path(value)
    if not path.is_absolute():
        raise PathError("result path must be absolute")
    return path
```

Resolve and validate the canonical result directory during `open`, use a
`threading.RLock`, index by both canonical path and random token, and scan only
directories containing `replay/manifest.json`. `discover()` returns summaries
without opening or assigning IDs; opening remains explicit.

- [ ] **Step 4: Run focused tests**

Run: `python3 -m unittest tests.test_replay_manifest tests.test_replay_web_paths tests.test_replay_web_runs -v`

Expected: all PASS.

- [ ] **Step 5: Record and commit path support**

```bash
git add CHANGELOG.md scripts/replay_web/paths.py scripts/replay_web/runs.py tests/test_replay_web_paths.py tests/test_replay_web_runs.py
git commit -m "feat: open replay results by local path"
```

---

### Task 5: Serve the central application and immutable bundle assets

**Files:**
- Create: `scripts/replay_web/server.py`
- Create: `tests/test_replay_web_server.py`
- Create: `replay.html`
- Create: `scripts/replay_web/static/replay.css`
- Create: `scripts/replay_web/static/api.js`
- Modify: `.gitignore`
- Modify: `justfile`
- Modify: `tests/test_justfile.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `RunRegistry` and safe manifest member resolution.
- Produces: `ServerConfig(repo_root, output_root, state_root, distro, host="127.0.0.1", port=8765)`.
- Produces: `create_server(config: ServerConfig) -> ThreadingHTTPServer`.
- Produces: `GET /api/config`, `GET /api/results`, `POST /api/runs/open`, `DELETE /api/runs/{id}`, `GET /api/runs/{id}/manifest`, `GET /api/runs/{id}/timeline/{index}`, and `GET /api/runs/{id}/images/{asset}`.
- Produces: `just replay-web host="127.0.0.1" port="8765"` while leaving bare `just` unchanged.

- [ ] **Step 1: Write HTTP contract tests using a real ephemeral port**

```python
server = create_server(ServerConfig(repo_root=repo, output_root=output, state_root=state, distro="Ubuntu-20.04", port=0))
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
self.addCleanup(server.shutdown)

config = get_json(base + "/api/config")
self.assertEqual(config["schema_version"], 1)
opened = post_json(base + "/api/runs/open", {"path": str(result)}, csrf=config["csrf_token"])
self.assertNotIn(str(result), opened["run_id"])
self.assertEqual(get_json(base + f"/api/runs/{opened['run_id']}/manifest")["dataset"]["kind"], "tum")
```

Assert same-origin/CSRF rejection on mutating routes, 404 for unknown IDs,
path traversal rejection, correct `Content-Encoding: gzip` and immutable cache
headers for chunks, image MIME types, and that `/etc/passwd` cannot be fetched
through any asset route.

- [ ] **Step 2: Verify server tests fail**

Run: `python3 -m unittest tests.test_replay_web_server -v`

Expected: FAIL because the server does not exist.

- [ ] **Step 3: Implement the standard-library service**

Use `ThreadingHTTPServer` and one `BaseHTTPRequestHandler` subclass created by
`make_handler(config, registry, csrf_token)`. Route on parsed path segments;
read JSON with a hard 64 KiB request limit; write all JSON through:

```python
def send_json(handler, status: int, value: Mapping[str, Any]) -> None:
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)
```

Serve only root `replay.html`, files below `scripts/replay_web/static/`, and
members already validated from an open run. Reject a configured host other than
`127.0.0.1` or `localhost`. Generate one startup CSRF token and require both
`X-Replay-CSRF` and an allowed `Origin` for `POST`/`DELETE`.

- [ ] **Step 4: Add the minimal shell and API client**

`replay.html` must load `/static/replay.css` and `/static/api.js` and contain
`#result-path`, `#open-result`, `#result-list`, `#tabs`, `#workspace`, and
`#global-status`. Implement:

```javascript
export class ReplayApi {
  async config() { return this.request('/api/config'); }
  async discover() { return this.request('/api/results'); }
  async open(path) { return this.request('/api/runs/open', {method: 'POST', body: {path}}); }
  async manifest(runId) { return this.request(`/api/runs/${encodeURIComponent(runId)}/manifest`); }
  chunkUrl(runId, index) { return `/api/runs/${encodeURIComponent(runId)}/timeline/${index}`; }
  imageUrl(runId, asset) { return `/api/runs/${encodeURIComponent(runId)}/images/${encodeURIComponent(asset)}`; }
}
```

The request helper sets CSRF only for mutations and turns non-2xx JSON errors
into an `ApiError(status, message)`.

- [ ] **Step 5: Add recipe and ignore local state**

Add `/.replay-web/` to `.gitignore`. Add a `replay-web` recipe that invokes
`python3 -m scripts.replay_web.server --host {{host}} --port {{port}}`, and a
justfile test asserting the URL, module and unchanged default recipe.

- [ ] **Step 6: Run HTTP and recipe tests**

Run:

```bash
python3 -m unittest tests.test_replay_web_server tests.test_justfile -v
just --dry-run replay-web
```

Expected: all PASS and the dry run binds `127.0.0.1:8765`.

- [ ] **Step 7: Record and commit the service shell**

```bash
git add .gitignore CHANGELOG.md justfile replay.html scripts/replay_web/server.py scripts/replay_web/static/replay.css scripts/replay_web/static/api.js tests/test_replay_web_server.py tests/test_justfile.py
git commit -m "feat: serve the central replay workspace"
```

---

### Task 6: Implement multi-result tabs and persistent workspace state

**Files:**
- Create: `scripts/replay_web/static/app.js`
- Create: `tests/test_replay_workspace_browser.py`
- Modify: `replay.html`
- Modify: `scripts/replay_web/static/replay.css`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `ReplayApi` from Task 5.
- Produces: `WorkspaceApp` with `openPath(path)`, `activate(tabId)`, `close(tabId)`, `move(tabId, beforeTabId)`, `restore()`, and `persist()`.
- Produces: tab state `{tabId, canonicalPath, runId, title, manifest, active, viewerState}` stored under local-storage key `daaam.replay.workspace.v1`.
- Produces: one `<section class="result-pane">` per open result; inactive panes have `hidden` set.

- [ ] **Step 1: Add a browser fixture for two valid served results**

```python
def test_two_results_keep_independent_tabs_and_restore_after_reload(self):
    self.open_workspace()
    self.open_result(self.tum_result)
    self.open_result(self.coda_result)
    tabs = self.driver.find_elements(By.CSS_SELECTOR, "[role=tab]")
    self.assertEqual([tab.text for tab in tabs], ["TUM / fr1_desk", "CODa / 0"])
    tabs[0].click()
    self.driver.refresh()
    self.wait_for_tabs(2)
    self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, "[role=tab][aria-selected=true]").text, "TUM / fr1_desk")
```

Add cases for duplicate open focusing the existing tab, tab close not deleting
files/history, drag/reorder persistence, one invalid restored path being skipped
without losing the valid tab, and result-list/default-output opening.

- [ ] **Step 2: Run the opt-in browser test and see the missing app**

Run: `RUN_BROWSER_TESTS=1 python -m unittest tests.test_replay_workspace_browser -v`

Expected: FAIL because tabs are not implemented.

- [ ] **Step 3: Implement `WorkspaceApp` and restoration**

```javascript
export class WorkspaceApp {
  constructor(api, storage = window.localStorage) {
    this.api = api;
    this.storage = storage;
    this.tabs = new Map();
    this.activeTabId = null;
  }

  async openPath(path, saved = {}) {
    const opened = await this.api.open(path);
    const existing = [...this.tabs.values()].find(tab => tab.canonicalPath === opened.canonical_path);
    if (existing) return this.activate(existing.tabId);
    const manifest = await this.api.manifest(opened.run_id);
    return this.addTab({canonicalPath: opened.canonical_path, runId: opened.run_id, manifest, saved});
  }
}
```

Use a client-only random `tabId`; never persist server `runId`. Restore paths in
saved order, then activate the saved path after all open attempts settle. Keep
errors in `#global-status` and do not clear successful tabs. Apply WAI-ARIA tab
roles and keyboard Left/Right/Delete behaviour.

- [ ] **Step 4: Make inactive tabs lightweight at the shell boundary**

Dispatch `replay-tab-activated` and `replay-tab-deactivated` custom events on
the pane before toggling `hidden`. The viewer added in Task 7 consumes those
events. Do not implement rendering in this task; each pane shows manifest
summary, dataset badge and an explicit loading placeholder.

- [ ] **Step 5: Run browser and static syntax tests**

Run:

```bash
node --check scripts/replay_web/static/api.js
node --check scripts/replay_web/static/app.js
RUN_BROWSER_TESTS=1 python -m unittest tests.test_replay_workspace_browser -v
```

Expected: PASS.

- [ ] **Step 6: Record and commit tabs**

```bash
git add CHANGELOG.md replay.html scripts/replay_web/static/app.js scripts/replay_web/static/replay.css tests/test_replay_workspace_browser.py
git commit -m "feat: open multiple replay result tabs"
```

---

### Task 7: Port the existing WebGL viewer to an HTTP chunk data source

**Files:**
- Create: `scripts/replay_web/static/viewer.js`
- Create: `scripts/replay_web/static/replay_data.js`
- Modify: `scripts/replay_web/static/app.js`
- Modify: `scripts/replay_web/static/replay.css`
- Modify: `replay.html`
- Modify: `tests/test_replay_browser.py`
- Modify: `tests/test_replay_reference_mesh.py`
- Modify: `tests/test_replay_workspace_browser.py`
- Delete: `scripts/replay_viewer.html`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: manifest/chunk/image APIs from Task 5 and tab lifecycle events from Task 6.
- Produces: `ReplayDataSource(api, runId, manifest)` with `snapshot(index)`, `image(kind, index)`, `descriptionImage(frameId)`, `referenceMesh()`, `clearDecoded()`.
- Produces: `ReplayViewer(host, dataSource, initialState)` with `mount()`, `activate()`, `deactivate()`, `serializeState()`, and `destroy()`.
- Preserves: mesh modes, stable label modes, selected-box overlay, catalog, entity visibility, picking, timeline timing and exact source-frame modal.

- [ ] **Step 1: Convert existing browser fixtures to launch the service**

Build a test bundle through `finalize_bundle`, start the ephemeral server, open
root `/replay.html`, then open the fixture result through the UI. Keep the
existing assertions for playback timing, entity selection, exact source frame,
reference/dynamic mesh switching, stable labels and selected-box visibility.
Add this explicit network assertion:

```python
requests = self.driver.execute_script("return window.__replayTestRequests")
self.assertLessEqual(len([url for url in requests if "/timeline/" in url]), 2)
self.assertFalse(any("data:image" in source for source in self.page_sources()))
```

- [ ] **Step 2: Run browser tests and confirm the placeholder cannot replay**

Run: `RUN_BROWSER_TESTS=1 python -m unittest tests.test_replay_browser tests.test_replay_workspace_browser -v`

Expected: FAIL before timeline and WebGL assertions.

- [ ] **Step 3: Implement the bounded chunk data source**

```javascript
export class ReplayDataSource {
  constructor(api, runId, manifest, maxChunks = 3) {
    this.api = api;
    this.runId = runId;
    this.manifest = manifest;
    this.maxChunks = maxChunks;
    this.decoded = new Map();
  }

  async snapshot(index) {
    const descriptor = this.manifest.chunks.find(chunk =>
      index >= chunk.first_snapshot && index < chunk.first_snapshot + chunk.snapshot_count);
    if (!descriptor) throw new RangeError(`snapshot ${index} is outside the replay`);
    const records = await this.loadChunk(descriptor);
    return records[index - descriptor.first_snapshot];
  }
}
```

Fetch the gzip response, parse JSON after browser decompression, decode the
existing deflate-packed array payload, and keep a true three-entry LRU. Prefetch
only the next/previous chunk around the playhead. `clearDecoded()` drops decoded
chunks and image object URLs.

- [ ] **Step 4: Port the viewer as an instance, not globals**

Move CSS/DOM structure and JavaScript behaviour from
`scripts/replay_viewer.html` into `ReplayViewer`. Replace `window.REPLAY`, global
element IDs and global state `S` with instance fields rooted below `host`.
Prefix querying with `this.host.querySelector`; each tab must be able to contain
the same control names without duplicate document IDs. Keep shader source,
quantization decode, picking, labels, selection overlay, camera orbit and
Rerun-independent behaviour byte-for-byte where possible.

`deactivate()` must cancel animation frames, pause playback, release transient
image object URLs and stop prefetch. It may keep WebGL buffers. `activate()`
resumes only if playback was active before deactivation. `destroy()` deletes
buffers, listeners and caches.

- [ ] **Step 5: Preserve selection and source-frame semantics**

Keep these two separate calls:

```javascript
selectEntity(kind, id, {revealInList: true});       // never calls setTime/setSnapshot
openDescriptionSource(catalogItem, sourceMetadata); // opens modal only
```

The source button resolves `manifest.images.descriptions[frameId]` through
`ReplayApi.imageUrl`. Missing provenance disables the button and says `无描述源帧`.

- [ ] **Step 6: Remove the legacy template only after parity tests pass**

Update source-level reference-mesh tests to inspect `viewer.js` and bundle
reference metadata. Delete `scripts/replay_viewer.html` and all `build` wording.
Search must return no product reference:

```bash
rg -n "replay_viewer\.html|replay\.py build|window\.REPLAY" scripts tests justfile replay.html
```

Expected: no matches except historical changelog/spec/plan text outside the searched paths.

- [ ] **Step 7: Run replay and browser suites**

Run:

```bash
docker compose run --rm -T daaam-ws python -m unittest \
  tests.test_replay_manifest tests.test_replay_bundle tests.test_replay_datasets tests.test_replay_catalog \
  tests.test_replay_reference_mesh tests.test_replay_rrd tests.test_run_replay -v
RUN_BROWSER_TESTS=1 python -m unittest tests.test_replay_browser tests.test_replay_workspace_browser -v
```

Expected: all PASS.

- [ ] **Step 8: Record and commit the viewer migration**

```bash
git add -A CHANGELOG.md replay.html scripts/replay_viewer.html scripts/replay_web/static tests/test_replay_browser.py tests/test_replay_reference_mesh.py tests/test_replay_workspace_browser.py
git commit -m "feat: load replay timelines in the central viewer"
```

---

### Task 8: Make current-time camera cards movable and tab-aware

**Files:**
- Modify: `scripts/replay_web/static/viewer.js`
- Modify: `scripts/replay_web/static/replay.css`
- Modify: `tests/test_replay_browser.py`
- Modify: `tests/test_replay_workspace_browser.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: per-tab `viewerState` persistence from Tasks 6–7.
- Produces: camera layout `{cameraKey: {x, y, z}}` serialized by `ReplayViewer.serializeState()`.
- Produces: pointer drag on `.camera-card__handle`, click-to-front, viewport clamping and `重置相机布局`.

- [ ] **Step 1: Add failing real-pointer tests**

```python
card = self.driver.find_element(By.CSS_SELECTOR, ".camera-card[data-camera=rgb]")
before = card.rect
ActionChains(self.driver).move_to_element(card.find_element(By.CSS_SELECTOR, ".camera-card__handle")).click_and_hold().move_by_offset(-120, -80).release().perform()
after = card.rect
self.assertNotEqual((before["x"], before["y"]), (after["x"], after["y"]))
self.assertEqual(self.current_timeline_value(), original_time)
```

Reload and assert the position returns; switch tabs and assert the other tab has
its own position; resize the viewport and assert every edge remains visible;
click reset and assert default positions.

- [ ] **Step 2: Run and confirm no drag behaviour**

Run: `RUN_BROWSER_TESTS=1 python -m unittest tests.test_replay_browser tests.test_replay_workspace_browser -v`

Expected: new camera-layout tests FAIL.

- [ ] **Step 3: Implement pointer capture and state persistence**

```javascript
handle.addEventListener('pointerdown', event => {
  handle.setPointerCapture(event.pointerId);
  this.drag = {key, pointerId: event.pointerId, startX: event.clientX,
    startY: event.clientY, origin: {...this.cameraLayout[key]}};
  this.raiseCamera(key);
});
```

On `pointermove`, clamp `x` to `[0, hostWidth-cardWidth]` and `y` to
`[toolbarHeight, hostHeight-cardHeight]`; update by `transform: translate(...)`
without causing timeline/render changes. Persist through the existing debounced
workspace save callback. On `pointercancel`/`pointerup`, release capture and
clear drag state. Keyboard users can move a focused handle in 10 px Arrow steps
and 1 px with Shift.

- [ ] **Step 4: Run tests and commit**

Run: `RUN_BROWSER_TESTS=1 python -m unittest tests.test_replay_browser tests.test_replay_workspace_browser -v`

Expected: PASS.

```bash
git add CHANGELOG.md scripts/replay_web/static/viewer.js scripts/replay_web/static/replay.css tests/test_replay_browser.py tests/test_replay_workspace_browser.py
git commit -m "feat: move replay camera cards"
```

---

### Task 9: Document and verify the core migration end to end

**Files:**
- Modify: `USAGE.md`
- Modify: `DOCKER.md`
- Modify: `CHANGELOG.md`
- Modify: `justfile`

**Interfaces:**
- Consumes: all core tasks.
- Produces: one documented workflow: run/finalize → `just replay-web` → open result tab.
- Produces: `just test` includes all new non-browser tests and both browser modules when Selenium is available.

- [ ] **Step 1: Add all core tests to the recipe before running it**

Add host tests for manifest/path/run/server logic and container tests for bundle
encoding. Add `tests.test_replay_workspace_browser` to the optional Selenium
command. Keep the existing fallback message when Selenium is unavailable.

- [ ] **Step 2: Update documentation with copyable commands**

Document:

```bash
just tum-run 30 fr1_desk
just replay-web
# open http://127.0.0.1:8765/replay.html
```

Describe `replay/manifest.json`, accepted Linux/Windows/WSL path forms,
multi-result tabs, camera layout reset, no legacy HTML compatibility, optional
`replay.rrd`, and recovery from a retained `.replay-recording` by running the
exact `python scripts/replay.py finalize ...` command. Remove instructions to
open generated HTML directly.

- [ ] **Step 3: Run the complete automated core suite**

Run: `just test`

Expected: all host and container unittest suites PASS; browser test either
passes or prints only the documented Selenium skip.

- [ ] **Step 4: Run a real small TUM smoke test**

Run:

```bash
just tum-test 30 fr1_desk
find output/tum_rrd_test -path '*/replay/manifest.json' -type f -print -quit | grep .
! find output/tum_rrd_test -name replay.html -print -quit | grep .
just replay-web
```

With the server running, open the newest result, scrub the timeline, select an
object without moving time, open its description frame when present, drag both
camera cards, open a second fixture/result tab, and return to the first tab.
Record the exact result directory used in the verification note; do not claim
CODa geometry accuracy from a synthetic fixture.

- [ ] **Step 5: Verify a CODa static bundle with explicit bag metadata**

Create a short CODa bag through `just coda-bag`, run it only if the current
machine has the required real data/models, and verify its new result contains a
one-snapshot `replay/manifest.json` whose dataset kind, sequence, start frame,
FPS and time origin match the bag sidecar. Open TUM and CODa simultaneously in
two tabs. If a fresh CODa pipeline run is impractical, run the real CODa dataset
adapter plus synthetic-DSG integration fixture and state clearly that the
full-pipeline CODa smoke was not run.

- [ ] **Step 6: Validate the changelog and repository diff**

Run:

```bash
git diff --check
git status --short
rg -n "replay\.html" scripts/run_replay.sh justfile USAGE.md DOCKER.md
```

Expected: references describe only the single workspace entry, and the
current-date changelog accurately covers every core modification.

- [ ] **Step 7: Commit documentation and test routing**

```bash
git add CHANGELOG.md USAGE.md DOCKER.md justfile
git commit -m "docs: explain the unified replay workspace"
```
