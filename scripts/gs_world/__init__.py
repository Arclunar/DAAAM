"""3D Gaussian splatting test worlds: train a splat model on posed RGB-D, render new RGB-D sequences from it.

The model is trained with gsplat (3DGS-MCMC densification, anti-aliased rasterization, sensor-depth supervision and
per-image exposure compensation) on the folder layout that DAAAM's ImageSequenceDataset reads (rgb/, depth/,
pose/poses.txt, camera_info.json). Rendered sequences are written in the same layout, so the DAAAM pipeline maps them
like any TUM sequence. ROS1 bags of a RealSense D455 are converted into that layout by rosbag_to_rgbd.

gsplat is installed into its own directory of the workspace volume (`just gs-install`), not into the DAAAM virtual
environment; every entry point calls env.activate() before importing it. Run the entry points in the daaam-ws
container from the repository root, e.g. `python -m scripts.gs_world.train tum_fr1_room` (see USAGE.md 9.7). `view` shows a
trained model in the browser (`just gs-view`).

With JELLYMAP_ROOT set (a JellyMap workspace), the modules are loaded from the shared copy in
$JELLYMAP_ROOT/sm-lab/datasets/scripts/gs_world; otherwise from this directory.
"""

import os as _os

_shared = _os.path.join(_os.environ.get('JELLYMAP_ROOT', ''), 'sm-lab/datasets/scripts/gs_world')
if _os.environ.get('JELLYMAP_ROOT') and _os.path.isfile(_os.path.join(_shared, '__init__.py')):
    __path__ = [_shared]
