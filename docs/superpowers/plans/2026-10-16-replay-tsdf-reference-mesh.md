# Replay 1 cm TSDF Reference Mesh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and cache a 1 cm Open3D TSDF reference mesh for each RGB-D dataset, embed it as an optional replay layer, stabilize floating labels, add a global label-hide mode, and make selected bounding boxes unmistakable.

**Architecture:** A new standalone builder owns dataset validation, fingerprints, Open3D integration and atomic cache publication. `run_replay.sh` applies the `REFERENCE_MESH` policy and passes only a validated `.npz` path into `replay.py`, which embeds compact reference arrays without importing Open3D. The existing offline viewer switches reusable WebGL buffers between static reference and dynamic Hydra meshes, while HTML overlays own stable labels and the selected-box highlight.

**Tech Stack:** Python 3.12, NumPy `<2`, OpenCV, Open3D 0.19, Bash, zlib/base64 packed replay data, plain WebGL and DOM/SVG overlays, `unittest` and optional Selenium.

**Spec:** `docs/superpowers/specs/2026-10-16-replay-tsdf-reference-mesh-design.md`

## Global Constraints

- The reference reconstruction uses `0.01 m` voxels, `0.04 m` truncation and `4.0 m` maximum depth.
- Stored poses are `T_world_camera`; invert each pose before Open3D integration.
- Call the artifact “1 cm reference mesh / 1 cm 参考网格”, never a ground-truth mesh.
- Do not decimate or silently simplify the reference mesh.
- `REFERENCE_MESH=auto` is the default; `off` skips it and `rebuild` forces replacement.
- The replay HTML remains self-contained and makes no network request.
- Preserve Object/Place selection, automatic list scrolling, per-entity hiding, Rerun output and the existing Hydra mesh profiles.
- Preserve unrelated user changes in the dirty shared worktree. Do not create a commit from this checkout unless the user explicitly asks; use scoped diffs as checkpoints.
- Every repository modification must be represented under `2026-10-16` in `CHANGELOG.md`.

---

### Task 1: Dataset Fingerprint and Cache Contract

**Files:**
- Create: `scripts/build_reference_mesh.py`
- Create: `tests/test_reference_mesh.py`

**Interfaces:**
- Produces: `ReferenceConfig(voxel_length, sdf_trunc, max_depth)`.
- Produces: `dataset_inventory(dataset: Path) -> dict`.
- Produces: `dataset_fingerprint(inventory: dict, config: ReferenceConfig) -> str`.
- Produces: `resolve_cache_paths(dataset: Path, override: Path | None) -> tuple[Path, Path]`.
- Produces: `load_valid_cache(dataset, config, override=None) -> Path | None`.

- [ ] **Step 1: Write failing fingerprint and invalidation tests**

Create a prepared-dataset fixture with `camera_info.json`, `timestamps.txt`,
`pose/poses.txt`, and paired `rgb/000000.png` / `depth/000000.png`. Assert that
two inventories produce the same digest, then mutate pose content, camera
content, a timestamp, a frame filename, a file size and each reconstruction
parameter and assert that every mutation changes the digest. Assert the default
cache paths and a `REFERENCE_MESH_CACHE` override:

```python
config = module.ReferenceConfig(0.01, 0.04, 4.0)
first = module.dataset_fingerprint(module.dataset_inventory(dataset), config)
(dataset / "pose" / "poses.txt").write_text("changed\n")
self.assertNotEqual(first, module.dataset_fingerprint(module.dataset_inventory(dataset), config))
self.assertEqual(
    module.resolve_cache_paths(dataset, None)[0],
    dataset / ".daaam/reference_mesh/open3d_tsdf_1cm.npz",
)
```

- [ ] **Step 2: Run the focused test and verify the intended failure**

Run: `python -m unittest tests.test_reference_mesh.ReferenceMeshCacheTest -v`

Expected: import/file failure because `scripts/build_reference_mesh.py` does
not exist yet.

- [ ] **Step 3: Implement deterministic inventory, digest and manifest validation**

Use frozen dataclasses and SHA-256 over canonical JSON. Hash the full contents
of the three small metadata files; for image sequences record relative path,
byte size and `st_mtime_ns` in sorted order. Validate required `.npz` members,
shapes, dtypes, finiteness and triangle bounds without importing Open3D:

```python
@dataclass(frozen=True)
class ReferenceConfig:
    voxel_length: float = 0.01
    sdf_trunc: float = 0.04
    max_depth: float = 4.0

def resolve_cache_paths(dataset: Path, override: Path | None = None):
    root = override if override else dataset / ".daaam" / "reference_mesh"
    return root / "open3d_tsdf_1cm.npz", root / "open3d_tsdf_1cm.json"

def dataset_fingerprint(inventory, config):
    payload = {"schema": CACHE_SCHEMA, "inventory": inventory, "config": asdict(config)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
```

- [ ] **Step 4: Verify green and inspect the scoped diff**

Run: `python -m unittest tests.test_reference_mesh.ReferenceMeshCacheTest -v`

Expected: all cache-contract tests pass. Then run
`git diff --check -- scripts/build_reference_mesh.py tests/test_reference_mesh.py`.

---

### Task 2: Open3D TSDF Reconstruction and Atomic Publication

**Files:**
- Modify: `scripts/build_reference_mesh.py`
- Modify: `tests/test_reference_mesh.py`
- Modify: `requirements.txt`
- Modify: `docker/setup_workspace.sh`

**Interfaces:**
- Consumes: cache helpers from Task 1.
- Produces: `read_prepared_dataset(dataset: Path) -> DatasetInputs`.
- Produces: `integrate_reference_mesh(inputs, config) -> dict[str, np.ndarray]`.
- Produces: `build_or_reuse(dataset, config, cache_dir=None, force=False) -> Path`.
- Produces CLI: `python scripts/build_reference_mesh.py DATASET [--cache-dir DIR] [--force]`.

- [ ] **Step 1: Write failing pose, reconstruction and reuse tests**

Create a two-frame 32×24 synthetic RGB-D fixture with constant 1 m depth,
known intrinsics and known `T_world_camera`. Patch only the Open3D integration
boundary for the pose-convention unit test and assert it receives
`np.linalg.inv(T_world_camera)`. When Open3D is installed, integrate the tiny
fixture and assert non-empty finite vertices, colours, normals and in-range
triangles. Run twice and assert the second call leaves cache and manifest mtimes
unchanged. Corrupt the manifest and assert it rebuilds.

- [ ] **Step 2: Run the focused tests and verify missing integration failures**

Run: `python -m unittest tests.test_reference_mesh.ReferenceMeshBuildTest -v`

Expected: failure because integration and publication functions are absent.

- [ ] **Step 3: Implement RGB-D loading and TSDF integration**

Read `camera_info.json`, sorted paired frame paths and one 4×4 row-major pose
per frame. Construct Open3D images from RGB arrays converted BGR→RGB and raw
`uint16` depth. Integrate with:

```python
volume = o3d.pipelines.integration.ScalableTSDFVolume(
    voxel_length=config.voxel_length,
    sdf_trunc=config.sdf_trunc,
    color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8,
)
rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
    color, depth,
    depth_scale=inputs.depth_scale,
    depth_trunc=config.max_depth,
    convert_rgb_to_intensity=False,
)
volume.integrate(rgbd, intrinsic, np.linalg.inv(T_world_camera))
```

Clean the extracted mesh, compute normals, convert to the cache dtypes and
publish `.npz` plus manifest through temporary siblings followed by
`os.replace`. Include integrated/skipped counts, bounds, versions and topology
counts in the manifest. Print only the final cache path on stdout; progress and
warnings go to stderr so `run_replay.sh` can capture the path reliably.

- [ ] **Step 4: Add the replay dependency to environment setup**

Add `open3d==0.19.0` to `requirements.txt`. Extend the setup smoke-import list
to include `open3d`, without changing the FoundationStereo wrapper's deliberate
local stub. Document in the setup comment that the wheel is large and required
only for reference-mesh construction.

- [ ] **Step 5: Verify the tiny reconstruction and direct CLI**

Run:

```bash
python -m unittest tests.test_reference_mesh.ReferenceMeshBuildTest -v
python scripts/build_reference_mesh.py datasets/tum_fr1_desk
python scripts/build_reference_mesh.py datasets/tum_fr1_desk
```

Expected: tests pass; the first real command reports integrated frames and
writes the cache atomically; the second reports cache reuse and does not change
cache timestamps.

---

### Task 3: Embed the Optional Static Reference Mesh

**Files:**
- Modify: `scripts/replay.py`
- Create: `tests/test_replay_reference_mesh.py`

**Interfaces:**
- Consumes: validated `.npz` path from Task 2 or `run_replay.sh`.
- Produces: `load_reference_mesh(path: Path) -> dict[str, np.ndarray]`.
- Produces: `quantize_positions(positions) -> tuple[np.ndarray, dict]` shared by dynamic and reference payload creation.
- Extends CLI: `replay.py build ... --reference-mesh PATH`.
- Extends `window.REPLAY`: optional `reference_mesh` with `quant`, `chunk`, `vertices`, `triangles` and display metadata.

- [ ] **Step 1: Write failing embedding and malformed-cache tests**

Build the existing minimal replay recording and a valid four-vertex reference
`.npz`. Assert the generated `window.REPLAY` includes independent reference
quantization and a compressed chunk that decodes to `positions`, `colors`,
`normals`, and `triangles`. Assert missing members, NaNs and out-of-range
triangle indices return non-zero with a clear path-specific error. Patch
`__import__` to reject `open3d` and prove HTML building from cache never imports
it.

- [ ] **Step 2: Run the test and verify `--reference-mesh` is rejected**

Run: `python -m unittest tests.test_replay_reference_mesh -v`

Expected: CLI failure for an unknown argument or missing loader.

- [ ] **Step 3: Implement cache loading, independent quantization and embedding**

Extract the current position quantization into a helper. Use the same packed
array format as snapshots, but preserve reference normals and use a separate
origin/scale:

```python
reference = None
if args.reference_mesh:
    arrays = load_reference_mesh(Path(args.reference_mesh))
    qpos, quant = quantize_positions(arrays.pop("positions"))
    arrays["mesh_pos"] = qpos
    arrays["mesh_col"] = arrays.pop("colors")
    arrays["mesh_nrm"] = arrays.pop("normals")
    arrays["mesh_faces"] = arrays.pop("triangles")
    reference = {
        "quant": quant,
        "chunk": base64.b64encode(zlib.compress(pack(arrays), 6)).decode(),
        "vertices": len(qpos),
        "triangles": len(arrays["mesh_faces"]),
        "label": "1 cm 全序列参考网格",
    }
```

Write the final HTML atomically and add reference counts, payload size and final
HTML size to build output.

- [ ] **Step 4: Verify green and legacy build compatibility**

Run:

```bash
python -m unittest tests.test_replay_reference_mesh tests.test_replay_catalog -v
python scripts/replay.py build FIXTURE_RECORDING --out /tmp/replay-dynamic-only.html
```

Expected: reference tests and catalog tests pass; a build without the new flag
still succeeds and contains no `reference_mesh` payload.

---

### Task 4: Automatic `auto/off/rebuild` Replay Policy

**Files:**
- Modify: `scripts/run_replay.sh`
- Modify: `tests/test_run_replay.py`
- Modify: `justfile`

**Interfaces:**
- Consumes: builder CLI from Task 2 and `--reference-mesh` from Task 3.
- Produces environment policy: `REFERENCE_MESH=auto|off|rebuild`.
- Consumes optional: `REFERENCE_MESH_CACHE=/writable/cache/directory`.

- [ ] **Step 1: Extend the fake-command shell harness with failing policy tests**

Record fake invocations in `tests/test_run_replay.py`. Assert `auto` invokes the
builder before HTML generation and passes the printed cache path; `off` invokes
neither builder nor flag; `rebuild` adds `--force`; automatic builder failure
still emits dynamic HTML; rebuild failure returns non-zero and retains raw
snapshots. Assert invalid values fail before the pipeline starts.

- [ ] **Step 2: Run the shell-policy tests and verify the expected failures**

Run: `python -m unittest tests.test_run_replay -v`

Expected: new policy assertions fail because the shell script has no reference
mesh handling.

- [ ] **Step 3: Implement the policy after recording completes**

Validate the mode near environment setup. After locating `run_dir`, resolve the
cache without affecting mapping runtime:

```bash
reference_args=()
case "${REFERENCE_MESH:-auto}" in
  off) ;;
  auto|rebuild)
    build_args=("${DATASET}")
    [ -z "${REFERENCE_MESH_CACHE:-}" ] || build_args+=(--cache-dir "${REFERENCE_MESH_CACHE}")
    [ "${REFERENCE_MESH}" != rebuild ] || build_args+=(--force)
    if reference_path="$(python scripts/build_reference_mesh.py "${build_args[@]}")"; then
      reference_args=(--reference-mesh "${reference_path}")
    elif [ "${REFERENCE_MESH}" = rebuild ]; then
      rc=1
    else
      echo "1 cm reference mesh unavailable; building dynamic-only replay" >&2
    fi
    ;;
  *) echo "REFERENCE_MESH must be auto, off, or rebuild" >&2; exit 2 ;;
esac
```

Pass `reference_args` only to `replay.py build`, never to Rerun generation.
Export `REFERENCE_MESH` and `REFERENCE_MESH_CACHE` through the `just replay`
container invocation so caller values are preserved.

- [ ] **Step 4: Verify all shell-policy cases**

Run: `python -m unittest tests.test_run_replay -v`

Expected: every legacy failure case and new policy case passes.

---

### Task 5: Mesh-Source Switching in the Offline Viewer

**Files:**
- Modify: `scripts/replay_viewer.html`
- Modify: `tests/test_replay_reference_mesh.py`
- Modify: `tests/test_replay_browser.py`

**Interfaces:**
- Consumes: optional `D.reference_mesh` from Task 3.
- Produces DOM control: `#mesh-mode` with `reference|dynamic|none`.
- Produces state: `S.reference`, `S.meshSource`, `S.uploadedMeshKey`.
- Produces debug API: active mesh mode and uploaded vertex/triangle counts.

- [ ] **Step 1: Write failing static and browser assertions**

Assert generated HTML contains `mesh-mode`, the reference warning text and no
remote URLs. In Selenium, open a reference-enabled fixture, assert default mode
`reference`, record uploaded topology counts, switch to `dynamic` and assert the
counts change, scrub the timeline and assert reference mode does not re-upload,
then choose `none` and assert no mesh draw occurs. A dynamic-only fixture must
omit the reference option and default to `dynamic`.

- [ ] **Step 2: Run focused tests and verify the missing-control failure**

Run:

```bash
python -m unittest tests.test_replay_reference_mesh -v
RUN_BROWSER_TESTS=1 python -m unittest tests.test_replay_browser -v
```

Expected: failure because `#mesh-mode` and reference buffer selection do not
exist.

- [ ] **Step 3: Decode the static payload once and select mesh buffers by key**

Add `decodeReference()` beside `snapshot()`. Generalize `uploadMesh(mesh, key)`
so it skips work when `key === S.uploadedMeshKey`. Decode reference positions
with `D.reference_mesh.quant`, use cached normals, and retain the existing
normal calculation for dynamic snapshots. Replace `#c-mesh` with:

```html
<div class="row"><span>网格</span><select id="mesh-mode">
  <option value="reference">1 cm 参考网格</option>
  <option value="dynamic">动态 Hydra 网格</option>
  <option value="none">隐藏网格</option>
</select></div>
<div id="mesh-status" class="hint"></div>
```

At startup remove the reference option when no payload exists. Keep brightness
and points-only controls applied to the chosen source. `fitView()` must include
the active reference bounds when reference mode is selected.

- [ ] **Step 4: Verify switching, fallback and self-containment**

Repeat the focused tests. Expected: reference is default when available,
dynamic-only pages retain their old view, and no browser console error occurs.

---

### Task 6: Stable Global Label Modes

**Files:**
- Modify: `scripts/replay_viewer.html`
- Modify: `tests/test_replay_browser.py`

**Interfaces:**
- Produces DOM control: `#label-mode` with `selected|all|none`.
- Produces: `rebuildLabelItems()` on snapshot/selection/visibility/mode changes.
- Produces: `positionLabels()` on camera draws without collision filtering.

- [ ] **Step 1: Write the failing browser interaction**

Build a snapshot with overlapping object, place and room anchors. In default
`selected` mode assert no cards before selection and exactly one after selecting
an object. In `all` mode capture each card's stable `data-label-key`, orbit and
zoom, then assert the key set is unchanged. In `none` mode assert every `.lab`
is hidden while the right-side entity rows remain visible. Hide an entity with
its eye button and assert only its label key disappears.

- [ ] **Step 2: Run and observe current collision-driven membership changes**

Run: `RUN_BROWSER_TESTS=1 python -m unittest tests.test_replay_browser.ReplayBrowserTest -v`

Expected: failure because `#label-mode` does not exist and `placeLabels()`
suppresses overlaps based on the current camera.

- [ ] **Step 3: Replace collision suppression with stable membership**

Replace `#c-objlab` with the three-mode selector. Give each label a stable key
(`object:<id>`, `place:<id>`, `room:<index>`, `building:<index>`). Rebuild the
member list only from scene-state changes; on draw, project each existing anchor
and update `transform`, hiding only when its own anchor is behind the camera or
outside the viewport. Append the selected member last and set its class to
`selected`. Remove `textWidth`, rectangle intersection and the 80-label cap.

- [ ] **Step 4: Verify stable membership and existing sidebar behaviour**

Run the focused browser test. Expected: label keys remain stable across camera
movement, global hide affects only floating cards, and selecting on the canvas
still scrolls the matching sidebar row into view.

---

### Task 7: High-Contrast Selected Bounding-Box Overlay

**Files:**
- Modify: `scripts/replay_viewer.html`
- Modify: `tests/test_replay_browser.py`

**Interfaces:**
- Consumes: selected visible object and the existing clip-space helpers.
- Produces overlay canvas: `#selection-overlay` with `pointer-events:none`.
- Produces: `drawSelectedBoxOverlay()` called after every WebGL draw.

- [ ] **Step 1: Write a failing selection-overlay browser assertion**

Select an object through the canvas and assert the overlay contains visible
clipped segments, uses amber `#ffb000`, a dark halo at least 8 px, a visible
stroke at least 4 px and corner markers. Switch bbox mode to `none` and assert
the overlay remains visible. Hide the object and assert the overlay clears.

- [ ] **Step 2: Run and verify the current white-line implementation fails**

Run: `RUN_BROWSER_TESTS=1 python -m unittest tests.test_replay_browser.ReplayBrowserTest.test_entity_browser_selects_absent_object_and_hides_it -v`

Expected: failure because selection is rendered as thin white WebGL lines and
there is no overlay.

- [ ] **Step 3: Implement clipped screen-space edges and corner emphasis**

Add a transparent 2D canvas over `#gl`, resize it with the WebGL canvas CSS
size, and clear it each frame. Project the eight bbox corners in clip space,
clip each `BOX_EDGES` segment against all six `CLIP_PLANES`, convert surviving
endpoints to CSS pixels, then draw twice: round-capped 10 px near-black halo and
4 px amber foreground. Draw 5 px amber corner circles with a 2 px dark outline.
Remove selected bbox edges from `G.highlightLines`; keep the selected node
highlight but change it from white to amber.

- [ ] **Step 4: Verify overlay and picking coexist**

Run the browser test. Expected: overlay remains visible through both mesh
sources, never intercepts clicks, respects entity hiding, and existing
frustum-clipped overlap picking still selects the intended smaller/nearer box.

---

### Task 8: Documentation, Changelog and End-to-End Verification

**Files:**
- Modify: `USAGE.md`
- Modify: `CHANGELOG.md`
- Modify: `justfile`

**Interfaces:**
- Documents: cache location, terminology, dependency size, modes, rebuild and
  read-only-dataset override.
- Produces: `just reference-mesh [seq]` for direct prebuild/inspection.

- [ ] **Step 1: Add the direct recipe and usage documentation**

Add a recipe that runs the builder against `/datasets/tum_<seq>` while passing
`REFERENCE_MESH_CACHE` when present. Document:

```bash
just reference-mesh fr1_desk
REFERENCE_MESH=off just replay 298
REFERENCE_MESH=rebuild just replay 298
REFERENCE_MESH_CACHE=/datasets/cache just replay 298
```

Explain that this is a full-sequence RGB-D reconstruction aligned by prepared
poses, not scene ground truth, and that the Open3D wheel and generated HTML can
be large.

- [ ] **Step 2: Replace the design-only changelog bullet with completed impact**

Under `2026-10-16`, describe the automatic 1 cm cache, HTML mesh selector,
stable label modes, global card hiding and high-contrast selected box. Mention
that the dynamic Hydra mesh remains available and no existing replay artifact
format is removed.

- [ ] **Step 3: Run focused automated verification**

Run:

```bash
python -m unittest tests.test_reference_mesh tests.test_replay_reference_mesh tests.test_replay_catalog tests.test_run_replay -v
RUN_BROWSER_TESTS=1 python -m unittest tests.test_replay_browser -v
git diff --check -- scripts/build_reference_mesh.py scripts/replay.py scripts/replay_viewer.html scripts/run_replay.sh requirements.txt docker/setup_workspace.sh justfile USAGE.md CHANGELOG.md tests/test_reference_mesh.py tests/test_replay_reference_mesh.py tests/test_run_replay.py tests/test_replay_browser.py
```

Expected: all focused tests pass and the diff check prints nothing.

- [ ] **Step 4: Build and inspect one real TUM artifact**

Run `REFERENCE_MESH=rebuild just replay 298`. Verify the cache manifest reports
`voxel_length=0.01`, `sdf_trunc=0.04`, finite non-zero topology, no skipped-frame
surprise and the current Open3D version. Open the produced `replay.html`, switch
reference/dynamic/hidden mesh modes, select two overlapping boxes, orbit with
`全部` labels and finally choose `隐藏` labels.

- [ ] **Step 5: Review final scope without touching unrelated changes**

Compare the scoped file list against this plan, check `CHANGELOG.md` covers every
user-visible modification, and report cache path, topology counts, HTML size and
verification results. Do not stage, commit or clean unrelated files.
