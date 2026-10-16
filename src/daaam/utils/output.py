"""Helpers for selecting pipeline result directories."""

from datetime import datetime
from pathlib import Path
from typing import Optional, Union


def resolve_pipeline_output_dir(
	root_dir: Path,
	configured_output_dir: Union[str, Path],
	exact_output_dir: Optional[Path] = None,
	timestamp: Optional[str] = None,
) -> Path:
	"""Return an exact caller-owned directory or create a timestamped standalone path."""
	if exact_output_dir is not None:
		return Path(exact_output_dir)

	stamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
	return Path(root_dir) / (configured_output_dir or "output") / f"out_{stamp}"
