# Replay 1 cm TSDF Reference Mesh and Stable Selection Design

## Purpose

Improve the self-contained replay page so dense scene graphs remain readable
and bounding-box placement can be judged against a detailed, reusable scene
surface. The feature adds a cached Open3D TSDF mesh reconstructed from the
dataset RGB-D frames and camera poses, then lets every replay HTML for that
dataset switch between this static reference and Hydra's time-varying mesh.

The reconstructed mesh is a **reference mesh**, not ground truth. TUM provides
ground-truth camera trajectories, but the surface is still estimated from noisy
depth images, calibration, pose interpolation and TSDF parameters. The UI and
documentation must use “1 cm reference mesh / 1 cm 参考网格”, never “ground-truth
mesh / 真值网格”.

## Scope

This task changes four connected parts of the existing replay workflow:

1. reconstruct and cache one 1 cm Open3D TSDF mesh per dataset;
2. embed the cache into generated offline HTML and switch mesh sources there;
3. make floating labels stable and provide one control that hides every label;
4. replace the faint white selected bounding box with a conspicuous overlay.

The existing Object/Place browser, per-entity eye controls, automatic list
scrolling, snapshot recording, Rerun output and Hydra 3 cm/5 cm profiles remain
unchanged. The reference reconstruction is independent of the number of replay
frames and does not replace Hydra during mapping.

## User Experience

### Mesh display

When a replay contains a cached reference mesh, the control panel exposes a
single mesh-mode selector:

- `1 cm 参考网格` — static full-sequence Open3D TSDF mesh, selected by default;
- `动态 Hydra 网格` — the existing mesh from the current replay snapshot;
- `隐藏网格` — neither mesh is drawn.

If no valid reference cache is available, the first option is absent and the
page defaults to the dynamic Hydra mesh. Brightening and points-only controls
apply to whichever mesh is active. A short status line identifies the selected
mesh and labels the reference as a full-sequence reconstruction so users do not
mistake it for the state available to Hydra at that historical frame.

### Floating labels

Replace the object-label checkbox with a global label-mode selector:

- `仅选中` — default; show only the selected Object or Place label;
- `全部` — show object, place, room and building labels for the snapshot;
- `隐藏` — hide every floating label card while leaving the right-side browser
  and detail panel available.

The `全部` mode does not use camera-dependent collision suppression. Its member
set is created when the snapshot, visibility state or label mode changes; orbit,
pan and zoom only update screen positions. Labels may overlap in a dense view,
but they must not disappear and reappear merely because another label crosses
them. Labels outside the viewport or behind the camera are hidden until their
own anchor becomes visible. The selected label is drawn last and above all
others. Per-entity eye controls continue to remove that entity's node, box,
edges and label.

### Selected bounding box

The selected object receives a screen-space overlay in vivid amber with a dark
halo, 4 px visible edges and emphasized corners. The overlay is depth
independent and follows the projected 3D box every frame, so mesh shading and
browser-dependent WebGL line-width limits cannot make it look like an ordinary
white edge. The normal 3D box rendering still follows the existing
`仅选中 / 全部 / 隐藏` bbox mode; the selection overlay appears whenever a visible
object is selected, including in `隐藏` bbox mode.

The overlay does not intercept pointer events. Existing frustum-clipped picking
and “prefer the smaller, nearer overlapping box” behaviour remain the source of
canvas selection.

## Reference Mesh Builder

Create `scripts/build_reference_mesh.py` as a focused command-line tool. It
accepts a prepared dataset with:

- `rgb/` and `depth/` image sequences;
- `camera_info.json` intrinsics and depth scale;
- `pose/poses.txt` containing `T_world_camera` for the prepared frames;
- `timestamps.txt` or the dataset's existing deterministic frame ordering.

The builder uses Open3D's CPU `ScalableTSDFVolume` with these fixed defaults:

- voxel length: `0.01 m`;
- signed-distance truncation: `0.04 m`;
- maximum integrated depth: `4.0 m`;
- RGB-D colour integration enabled;
- every valid prepared frame integrated exactly once.

Open3D expects a world-to-camera extrinsic, so each stored
`T_world_camera` pose is inverted before integration. Frames missing RGB, depth
or a finite pose are counted and skipped with a warning; calibration errors,
dimension mismatches and zero successfully integrated frames are fatal.

After integration, the builder extracts a triangle mesh, computes vertex
normals and removes duplicated vertices/triangles, degenerate triangles and
unreferenced vertices. It does not decimate the mesh: the 1 cm surface detail is
the purpose of the artifact.

The serialized cache contains compact NumPy arrays:

- `positions`: `float32[N, 3]` metres in the dataset world frame;
- `colors`: `uint8[N, 3]` sRGB vertex colours;
- `normals`: `float32[N, 3]`;
- `triangles`: `uint32[M, 3]`.

The cache is written atomically as
`<dataset>/.daaam/reference_mesh/open3d_tsdf_1cm.npz`, accompanied by
`open3d_tsdf_1cm.json`. `REFERENCE_MESH_CACHE` may point to another directory
for read-only datasets. The implementation adds a pinned Open3D dependency to
the replay environment and documents its large wheel size.

## Cache Identity and Reuse

The manifest stores:

- schema version and creation time;
- canonical dataset path and dataset fingerprint;
- RGB/depth frame count and deterministic relative file list;
- hashes of `camera_info.json`, `pose/poses.txt` and `timestamps.txt` when
  present;
- RGB/depth file sizes and nanosecond modification times;
- voxel length, truncation, maximum depth and pose convention;
- Python, NumPy and Open3D versions;
- integrated/skipped frame counts, bounds, vertex count and triangle count.

The dataset fingerprint is a SHA-256 digest of the reconstruction parameters,
metadata-file content hashes and ordered RGB/depth file metadata. This avoids
reading every multi-megabyte image merely to decide whether a cache can be
reused, while still invalidating on normal dataset edits. A cache is valid only
when the manifest schema, fingerprint, reconstruction parameters and required
arrays all match.

`scripts/run_replay.sh` supports:

- `REFERENCE_MESH=auto` (default): reuse a valid cache or build it once;
- `REFERENCE_MESH=off`: skip lookup/build and generate the legacy dynamic-only
  HTML;
- `REFERENCE_MESH=rebuild`: replace the cache even when valid.

Automatic reconstruction starts after the mapping run so it cannot reduce
mapping throughput. A failure in `auto` mode emits a clear warning and still
builds the replay with the dynamic Hydra mesh. A failure in `rebuild` mode is a
task failure because the user explicitly requested a new reference. Partial
`.npz` or manifest files are never published.

All later replay builds for the same dataset resolve the same valid cache. The
cache builder is also directly runnable, allowing a reference mesh to be
prepared before replay.

## Replay Data and Rendering

`scripts/replay.py build` gains `--reference-mesh PATH`. It validates the cache,
quantizes positions using a reference-specific origin and scale, preserves
indexed topology and colours, and embeds one compressed static mesh payload in
the existing offline HTML. The current per-snapshot Hydra payload remains
unchanged.

The viewer keeps one set of reusable WebGL buffers for the active mesh. Changing
mesh mode uploads either the static reference arrays or the current snapshot's
dynamic arrays, then recomputes normals only if they are absent. Snapshot
changes do not re-upload the reference mesh. Indexed meshes with more than
65,535 vertices continue to use the existing 32-bit index path; unsupported
browsers receive the existing point fallback plus an on-page status warning.

The page remains self-contained and makes no network request. Because a 1 cm
mesh may make the HTML substantially larger, build output reports the reference
vertex/triangle counts, compressed payload size and final HTML size. It warns
but does not silently simplify geometry.

## Components and Interfaces

### `scripts/build_reference_mesh.py`

Owns dataset discovery, fingerprints, manifest validation, pose conversion,
Open3D integration, mesh cleanup and atomic cache publication. It exposes pure
helpers for fingerprint/manifest tests and a CLI that prints the resolved cache
path on success.

### `scripts/run_replay.sh`

Owns the `REFERENCE_MESH` policy. It invokes the builder only when the dataset
is available and passes a validated cache to `replay.py build`. It retains the
existing replay recording failure semantics.

### `scripts/replay.py`

Owns cache-array validation, quantization and embedding. It does not import
Open3D; rebuilding an existing HTML from a valid cache therefore needs only the
normal lightweight replay dependencies.

### `scripts/replay_viewer.html`

Owns mesh-source switching, the stable label modes and the selected-box overlay.
It does not reconstruct geometry or infer cache validity.

## Error Handling

- Missing Open3D in `auto` mode explains how to install the replay dependency,
  omits the reference mesh and preserves the dynamic replay.
- A corrupt or stale cache is never embedded. `auto` rebuilds it; `rebuild`
  replaces it; `off` ignores it.
- An unwritable dataset cache directory produces a clear message suggesting
  `REFERENCE_MESH_CACHE`; automatic mode may still finish with dynamic mesh.
- Input image decode failures identify the relative frame and are included in
  skipped-frame totals. A high skipped ratio is reported prominently.
- Cache and HTML writers use a temporary sibling followed by atomic rename.
- Invalid array shapes, non-finite vertices, out-of-range triangle indices and
  empty meshes fail validation before HTML generation.

## Testing

Implementation follows test-driven development.

1. Pure unit tests cover deterministic fingerprints, invalidation after pose,
   camera, file-list, timestamp or reconstruction-parameter changes, cache-path
   overrides and corrupt manifest rejection.
2. A tiny synthetic RGB-D fixture with known intrinsics and poses verifies
   `T_world_camera` inversion, successful 1 cm Open3D integration, finite mesh
   arrays and cache reuse without rewriting.
3. Replay builder tests verify optional reference embedding, independent
   quantization, large indices, no Open3D import on cache consumption and clear
   rejection of malformed arrays.
4. Shell-policy tests cover `auto`, `off`, `rebuild`, graceful auto fallback and
   explicit rebuild failure.
5. Browser tests verify the three mesh modes, default reference selection,
   stable all-label membership during camera movement, global label hiding,
   selected-label priority, vivid overlay styling and preservation of canvas to
   sidebar selection/scrolling.
6. The existing replay catalog, browser, Rerun and 3 cm/5 cm profile tests must
   remain green.

The final verification includes a real TUM replay build, inspection of the
manifest statistics, opening the generated HTML, switching both mesh sources,
selecting overlapping boxes and confirming that orbiting no longer changes the
set of visible labels.

## Acceptance Criteria

- The first default replay for a writable dataset builds and stores a valid
  Open3D TSDF cache at 1 cm; later replays reuse it without reconstruction.
- Changing dataset camera/pose/frame metadata or reconstruction parameters
  invalidates the cache.
- Every generated HTML with a valid cache can switch among the reference mesh,
  dynamic Hydra mesh and no mesh without network access.
- The page clearly calls the static mesh a full-sequence 1 cm reference, not
  ground truth.
- Camera movement does not cause collision-based label disappearance or
  reappearance; one control can hide every floating card.
- A selected object's box is immediately obvious against both meshes and remains
  visible through them.
- Existing Object/Place selection, list auto-scroll, per-entity hiding, timeline
  playback, Rerun generation and coarse/HQ Hydra profiles keep working.
- Tests and a real TUM replay build pass, and `CHANGELOG.md` accurately records
  the completed user-visible changes.
