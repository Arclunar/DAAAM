# TODO: Replay 1 cm TSDF Reference Mesh and Viewer Readability

## Handoff Status

The user approved the design and asked for direct implementation. No production
implementation has started yet. The completed artifacts are:

- design: `docs/superpowers/specs/2026-10-16-replay-tsdf-reference-mesh-design.md`;
- implementation plan: `docs/superpowers/plans/2026-10-16-replay-tsdf-reference-mesh.md`;
- changelog entry under `2026-10-16` recording the design/plan stage.

Continue from the implementation plan. Do not restart requirements discovery
unless the repository has materially changed.

## User Request

The current replay page has too many overlapping bounding boxes and floating
description cards. Collision suppression makes cards appear/disappear and jump
while orbiting the view. The selected object's white bounding box is hard to
see. The user wants:

1. stable floating cards that do not flicker because another label overlaps;
2. a single control that hides all floating cards;
3. a conspicuous selected-object bounding box;
4. automatic construction and reuse of a detailed scene mesh so bbox placement
   can be judged against geometry;
5. Open3D TSDF for that reconstruction, with a **1 cm voxel size**;
6. future replay HTML files for the same dataset to offer this detailed mesh as
   an alternative to the existing Hydra mesh.

The user explicitly chose Open3D TSDF over Gaussian splatting and approved the
written design. Treat the approved spec as authoritative.

## Important Terminology

Call the generated surface a **1 cm reference mesh / 1 cm 参考网格**, not a
ground-truth mesh. TUM supplies ground-truth camera trajectories, but the mesh
is still reconstructed from noisy RGB-D data, calibration, interpolated poses
and TSDF parameters.

## Approved Behaviour

### Reference reconstruction

- Use Open3D CPU `ScalableTSDFVolume`.
- Voxel length: `0.01 m`.
- SDF truncation: `0.04 m`.
- Maximum integrated depth: `4.0 m`.
- Integrate the entire prepared dataset once, independent of replay frame count.
- Prepared poses are row-major `T_world_camera`; Open3D receives
  `inverse(T_world_camera)`.
- Extract an indexed coloured triangle mesh with normals and clean duplicate,
  degenerate and unreferenced topology.
- Do not decimate or silently simplify the mesh.

### Cache

- Default cache:
  `<dataset>/.daaam/reference_mesh/open3d_tsdf_1cm.npz`
  plus `open3d_tsdf_1cm.json`.
- `REFERENCE_MESH_CACHE` overrides the cache directory for read-only datasets.
- Fingerprint camera, pose and timestamp file contents plus ordered RGB/depth
  relative paths, file sizes and nanosecond mtimes, reconstruction parameters
  and schema version.
- Publish cache and manifest atomically.
- `REFERENCE_MESH=auto` is default: reuse a valid cache or build once.
- `REFERENCE_MESH=off`: dynamic Hydra mesh only.
- `REFERENCE_MESH=rebuild`: force cache replacement; reconstruction failure is
  fatal in this mode.
- Automatic-mode failure warns and still produces a dynamic-only replay.

### Replay HTML

- Mesh selector:
  - `1 cm 参考网格` (default when embedded);
  - `动态 Hydra 网格`;
  - `隐藏网格`.
- Reference mesh is static/full-sequence; Hydra mesh remains snapshot-dependent.
- Keep the page self-contained and offline.
- Use independent quantization metadata for the reference mesh.
- Reuse one WebGL buffer set and do not re-upload the static mesh on timeline
  changes.
- Report reference vertex/triangle counts, compressed payload size and final
  HTML size. Warn about size; do not simplify geometry automatically.

### Labels/cards

- Replace the object-label checkbox with a global label mode:
  - `仅选中` (default);
  - `全部`;
  - `隐藏`.
- In `全部`, label membership changes only on snapshot, selection, entity-eye
  visibility or mode changes. Orbit/pan/zoom only changes screen positions.
- Remove camera-dependent collision suppression and the 80-label cap. Overlap is
  acceptable in explicit `全部` mode; flicker is not.
- Hide an individual label only when its own anchor is behind the camera or
  outside the viewport.
- Draw the selected label last/above others.
- `隐藏` affects floating canvas cards only, not the right-side Object/Place
  browser or details.

### Selected bbox

- Replace thin white selected-box lines with a screen-space overlay.
- Use vivid amber (`#ffb000`) foreground, roughly 4 px, over a near-black halo
  of roughly 10 px, with emphasized corners.
- Keep it depth-independent and visible with either mesh.
- Show it whenever a visible object is selected, even when normal bbox mode is
  `隐藏`.
- Set `pointer-events:none`; preserve the current clipped bbox picking and
  smaller/nearer overlap preference.

## Existing Repository Context

Repository root:
`/home/arclunar/warmoon_ws/DAAAM`
(`\\wsl.localhost\Ubuntu-20.04\home\arclunar\warmoon_ws\DAAAM` from Windows).

Relevant existing files:

- `scripts/replay.py`
  - `build()` currently merges and quantizes the Hydra mesh per snapshot, packs
    chunks into a self-contained HTML, and supports `--dataset` camera images.
  - It has no reference-mesh argument yet.
- `scripts/replay_viewer.html`
  - already contains the Object/Place browser, persistent per-entity eye state,
    canvas selection, sidebar tab switching and immediate row scrolling;
  - bbox modes already are `selected/all/none` and default to selected;
  - current `placeLabels()` sorts projected labels, collision-culls them and
    caps them at 80, which causes the reported jumping;
  - selected boxes are current white depth-independent WebGL lines.
- `scripts/run_replay.sh`
  - records ZMQ snapshots, runs the TUM pipeline, then builds HTML and Rerun;
  - no reference-mesh policy exists yet.
- `scripts/prepare_tum_rgbd.py`
  - prepared layout is `rgb/`, `depth/`, `pose/poses.txt`,
    `timestamps.txt`, `camera_info.json`;
  - `camera_info.json` contains `width`, `height`, 3×3 `intrinsics`,
    `depth_scale`, and fps;
  - pose rows are 16 values encoding `T_world_camera`.
- `requirements.txt`
  - currently pins `numpy<2.0.0` and does not include Open3D;
  - planned dependency is `open3d==0.19.0`.
- `docker/setup_workspace.sh`
  - owns installation and smoke-import checks.
- `tests/test_replay_browser.py`
  - existing optional Selenium coverage for selection, clipped boxes, entity
    hiding, and sidebar behaviour.
- `tests/test_run_replay.py`
  - existing fake-command shell harness for replay failure semantics.
- `justfile`
  - has `replay`, `replay-hq`, `test`, `tum-test`, and Rerun recipes.

Existing 3 cm Hydra work (`tum_rgbd_khronos_hq.yaml`, `just replay-hq` and its
documentation/tests) belongs to another/current user change. Keep it intact.
The 1 cm Open3D reference is a separate full-sequence layer, not a replacement
for that profile.

## Worktree Safety

The worktree is very dirty and many relevant files are untracked/user-owned,
including `CHANGELOG.md`, `justfile`, `scripts/replay.py`,
`scripts/replay_viewer.html`, `scripts/run_replay.sh`, `config/hydra_config/`
and `tests/`. There are also unrelated tracked modifications.

- Do not reset, clean, overwrite, stage broadly, commit broadly or force-push.
- Do not create a normal Git worktree expecting the untracked replay files to
  appear there; they will not.
- Inspect each target before editing and use scoped patches.
- Do not commit unless the user explicitly requests it.
- Every repository modification must be reflected in `CHANGELOG.md` under the
  current date, per `AGENTS.md`.

## Implementation Order

Follow `docs/superpowers/plans/2026-10-16-replay-tsdf-reference-mesh.md`:

1. Add `scripts/build_reference_mesh.py` cache/fingerprint contract.
2. Add Open3D integration, cleanup and atomic publication.
3. Add optional reference-mesh loading/embedding to `scripts/replay.py`.
4. Add `REFERENCE_MESH=auto|off|rebuild` to `scripts/run_replay.sh`.
5. Add reference/dynamic/hidden mesh switching to the viewer.
6. Replace collision-culling labels with stable global modes.
7. Add the screen-space amber bbox overlay.
8. Update `USAGE.md`, `justfile` and the final `CHANGELOG.md` wording.

## Current Todo Checklist

- [ ] Inspect current scoped diffs immediately before editing; concurrent work
  may have changed the untracked replay files.
- [ ] Implement `scripts/build_reference_mesh.py`.
- [ ] Add/pin Open3D in the replay environment.
- [ ] Add reference mesh embedding in `scripts/replay.py`.
- [ ] Add replay shell policy and cache override.
- [ ] Implement mesh source selector.
- [ ] Implement stable global label modes.
- [ ] Implement high-contrast bbox overlay.
- [ ] Update usage documentation and replace the design-only changelog entry
  with completed user-visible behaviour.
- [ ] Run the scoped verification described in the implementation plan, or
  clearly report any verification skipped or blocked.
- [ ] Do a final scoped diff review and preserve all unrelated changes.

## Dependency and Runtime Notes

- A repository comment in `scripts/run_foundation_stereo_coda.py` notes that
  Open3D is a roughly 450 MB wheel and is deliberately stubbed there because
  FoundationStereo does not use it. Adding Open3D for reference reconstruction
  must not remove or alter that local stub behaviour.
- The TUM dataset mount in `compose.yaml` is `${DAAAM_DATASETS_DIR:-./datasets}`
  to `/datasets`, normally writable. Use `REFERENCE_MESH_CACHE` when it is not.
- A 1 cm full-sequence mesh can produce a large `.npz` and replay HTML. This is
  expected; report sizes instead of quietly decimating.
- The existing dynamic viewer supports 32-bit element indices; retain the
  existing point fallback/status path for unsupported browsers.

## Definition of Done

- First default replay builds a valid 1 cm cache; later replays reuse it.
- Cache invalidates when camera/pose/frame metadata or reconstruction parameters
  change.
- Reference/dynamic/hidden mesh switching works offline.
- Camera motion no longer changes label membership through collision culling.
- One selector hides all floating cards.
- Selected object bbox is immediately visible through either mesh.
- Existing Object/Place browser, row auto-scroll, per-entity eyes, timeline,
  Rerun generation and Hydra profiles remain intact.
- `CHANGELOG.md` accurately describes the implementation actually delivered.
