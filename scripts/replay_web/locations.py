"""Locate the algorithm checkout while replay tools live in a sibling workspace."""
import os
from pathlib import Path

def workspace_root():
    """Find the workspace independently of viewer nesting or legacy aliases."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "sm-lab").is_dir() and (candidate / "runs").is_dir():
            return candidate
    # Retain standalone legacy replay workspace layouts.
    return Path(__file__).resolve().parents[3]


def algorithm_root():
    configured = os.environ.get("DAAAM_REPO_DIR")
    if configured:
        candidate = Path(configured)
        if not (candidate / "compose.yaml").is_file():
            raise RuntimeError("DAAAM_REPO_DIR must name the algorithm checkout")
        return candidate
    candidates = [Path.cwd(), Path("/opt/daaam_ws/src/daaam"),
                  workspace_root() / "daaam_ws/src/daaam"]
    for candidate in candidates:
        if (candidate / "compose.yaml").is_file() and (candidate / "scripts/run_pipeline.py").is_file():
            return candidate
    raise RuntimeError("Cannot locate DAAAM; set DAAAM_REPO_DIR")
