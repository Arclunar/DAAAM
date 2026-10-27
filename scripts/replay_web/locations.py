"""Locate the algorithm checkout while replay tools live in a sibling workspace."""
import sys
from pathlib import Path

def workspace_root():
    """Find the workspace independently of viewer nesting or legacy aliases."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "sm-lab").is_dir() and (candidate / "runs").is_dir():
            return candidate
    # Retain standalone legacy replay workspace layouts.
    return Path(__file__).resolve().parents[3]


def algorithm_root():
    """Use the shared canonical host/container source lookup."""
    package_root = str(workspace_root() / "sm-lab/infra")
    if package_root not in sys.path:
        sys.path.insert(0, package_root)
    from workspace_common.paths import algorithm_root as shared_algorithm_root
    return shared_algorithm_root()
