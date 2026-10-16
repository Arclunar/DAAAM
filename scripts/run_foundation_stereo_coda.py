#!/usr/bin/env python3
"""Run FoundationStereo's CODa depth estimation (RUNNING.md 1.5) inside the DAAAM environment.

  python scripts/run_foundation_stereo_coda.py --dataset_folder $CODA_ROOT_DIR --sequence_id 0 [--depth_skip N ...]

All arguments go to run_coda_depth_estimation.py of the FoundationStereo checkout in $FOUNDATION_STEREO_DIR (default
/data/FoundationStereo; weights in its pretrained_models/, --ckpt_dir defaults to 11-33-40 as in that script). Two
things in the DAAAM venv would break the script as it is, and are handled here without touching the checkout:
  - its `core` package has no __init__.py, so the `core` package of Perception Encoder (installed for the CLIP frame
    selection) shadows it;
  - Utils.py imports open3d and trimesh at the top, which the depth estimation never uses (open3d is a 450 MB
    wheel): empty stub modules are used.
xformers is not needed either (DINOv2 falls back to PyTorch attention); XFORMERS_DISABLED=1 keeps it that way.

A full sequence takes hours (4.3 s per frame on an RTX 5060 Ti), so two things make long runs practical:
  - only the frames that exist for both cameras are used: the script pairs the sorted file lists of cam0 and cam1 by
    position, so a frame that is still being downloaded for one camera would shift every later pair. The frames that
    arrive later are picked up by the next run, which skips the depth maps that exist already;
  - if the file named in $STOP_FILE exists, the run ends cleanly before the next frame (the frame in progress is
    finished), which frees the GPU for another job within seconds; run it again later to continue.
"""
import os
import pathlib
import runpy
import sys
import types

import tqdm

fs_dir = os.path.abspath(os.environ.get("FOUNDATION_STEREO_DIR", "/data/FoundationStereo"))
script = os.path.join(fs_dir, "scripts", "run_coda_depth_estimation.py")
if not os.path.isfile(script):
    sys.exit("FoundationStereo checkout not found at %s (set FOUNDATION_STEREO_DIR)" % fs_dir)

os.environ.setdefault("XFORMERS_DISABLED", "1")
args = sys.argv[1:]
if not any(a == "--ckpt_dir" or a.startswith("--ckpt_dir=") for a in args):
    args += ["--ckpt_dir", os.path.join(fs_dir, "pretrained_models", "11-33-40", "model_best_bp2.pth")]

sys.path.insert(0, fs_dir)
core = types.ModuleType("core")
core.__path__ = [os.path.join(fs_dir, "core")]
sys.modules["core"] = core
for unused in ("open3d", "trimesh"):
    sys.modules.setdefault(unused, types.ModuleType(unused))


def frame_number(path):
    return int(path.stem.rsplit("_", 1)[1])


glob = pathlib.Path.glob


def paired_glob(self, pattern):
    """Path.glob that lists only the frames present in both 2d_rect/cam0/<seq> and 2d_rect/cam1/<seq>."""
    paths = list(glob(self, pattern))
    if pattern == "*.png" and self.parent.parent.name == "2d_rect" and self.parent.name in ("cam0", "cam1"):
        other = "cam1" if self.parent.name == "cam0" else "cam0"
        theirs = {frame_number(p) for p in glob(self.parent.parent / other / self.name, "*.png")}
        paths = [p for p in paths if frame_number(p) in theirs]
    return iter(paths)


pathlib.Path.glob = paired_glob

stop_file = os.environ.get("STOP_FILE")
if stop_file:
    class StoppableTqdm(tqdm.tqdm):
        def __iter__(self):
            for item in super().__iter__():
                if os.path.exists(stop_file):
                    print("%s exists: stopping before the next frame" % stop_file, flush=True)
                    self.close()
                    raise SystemExit(0)
                yield item

    tqdm.tqdm = StoppableTqdm

sys.argv = [script] + args
runpy.run_path(script, run_name="__main__")
