"""Results the replay workspace has opened, behind opaque process-local run IDs.

The browser names a result by its path once (`open`); everything after that uses the random `run_id`, which encodes
nothing about the path. Opening resolves the path (symlinks included), requires a readable directory with a valid
`replay/manifest.json` and registers it under its canonical path, so the same result opened twice, through a symlink or
with `.`/`..` steps, keeps one ID. Closing forgets the ID only; no file is touched. After a service restart the browser
opens its saved paths again and gets new IDs.

`discover` lists valid bundles below the default output root without opening them: a bounded walk that follows
symlinks (output/coda points to another drive on this machine) but never loops, and skips hidden directories and the
inside of bundles and results.

Standard library only, Python 3.8.
"""

import os
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from scripts.replay_web.manifest import BUNDLE_DIR, MANIFEST_NAME, ManifestError, bundle_root_of, validate_manifest
from scripts.replay_web.paths import PathError, normalize_user_path

DEFAULT_SCAN_DEPTH = 4


@dataclass(frozen=True)
class OpenRun:
	run_id: str
	canonical_path: Path
	bundle_root: Path
	manifest: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class OpenRunSummary:
	"""A valid bundle found by discovery: what the result list shows. It has no run ID until it is opened."""

	canonical_path: Path
	relative_path: str
	title: str
	dataset_kind: str
	sequence: str
	snapshot_count: int
	time_first: float
	time_last: float
	created_at: Optional[str]
	modified: float

	def to_json(self) -> Dict[str, Any]:
		return {
			"path": str(self.canonical_path),
			"relative_path": self.relative_path,
			"title": self.title,
			"dataset_kind": self.dataset_kind,
			"sequence": self.sequence,
			"snapshot_count": self.snapshot_count,
			"time_first": self.time_first,
			"time_last": self.time_last,
			"created_at": self.created_at,
			"modified": self.modified,
		}


def _result_directory(path: Path) -> Path:
	"""The result directory for a resolved path that may also name its bundle directory or the manifest itself."""
	if path.is_file() and path.name == MANIFEST_NAME and path.parent.name == BUNDLE_DIR:
		return path.parent.parent
	if path.is_dir() and path.name == BUNDLE_DIR and (path / MANIFEST_NAME).is_file():
		return path.parent
	return path


class RunRegistry:
	def __init__(self, output_root: Path, distro: str, max_depth: int = DEFAULT_SCAN_DEPTH):
		self.output_root = Path(output_root)
		self.distro = distro
		self.max_depth = max_depth
		self._lock = threading.RLock()
		self._by_id: Dict[str, OpenRun] = {}
		self._by_path: Dict[Path, str] = {}

	def canonical(self, raw_path: str) -> Path:
		"""The canonical result directory a user path names; PathError when it is not a readable directory."""
		path = normalize_user_path(raw_path, distro=self.distro)
		try:
			resolved = path.resolve(strict=True)
		except FileNotFoundError as error:
			raise PathError(f"{path} does not exist") from error
		except (OSError, RuntimeError) as error:
			raise PathError(f"{path} cannot be resolved ({error})") from error
		result = _result_directory(resolved)
		if not result.is_dir():
			raise PathError(f"{result} is not a directory (open a result directory that contains replay/manifest.json)")
		if not os.access(str(result), os.R_OK | os.X_OK):
			raise PathError(f"{result} is not readable")
		return result

	def open(self, raw_path: str) -> OpenRun:
		"""Validate and register a result; the same canonical result always keeps its ID while it stays open."""
		canonical = self.canonical(raw_path)
		bundle = bundle_root_of(canonical)
		manifest = validate_manifest(bundle)
		with self._lock:
			run_id = self._by_path.get(canonical)
			if run_id is None:
				run_id = secrets.token_urlsafe(18)
				while run_id in self._by_id:   # pragma: no cover - 144 random bits
					run_id = secrets.token_urlsafe(18)
				self._by_path[canonical] = run_id
			run = OpenRun(run_id=run_id, canonical_path=canonical, bundle_root=bundle, manifest=manifest)
			self._by_id[run_id] = run
			return run

	def get(self, run_id: str) -> OpenRun:
		with self._lock:
			return self._by_id[run_id]

	def find(self, canonical_path: Path) -> Optional[OpenRun]:
		with self._lock:
			run_id = self._by_path.get(Path(canonical_path))
			return self._by_id.get(run_id) if run_id else None

	def runs(self) -> List[OpenRun]:
		with self._lock:
			return list(self._by_id.values())

	def close(self, run_id: str) -> bool:
		"""Forget an open result; its files are never touched. False when the ID is unknown."""
		with self._lock:
			run = self._by_id.pop(run_id, None)
			if run is None:
				return False
			self._by_path.pop(run.canonical_path, None)
			return True

	def discover(self) -> List[OpenRunSummary]:
		"""Valid bundles below the output root, newest manifest first, each canonical result once."""
		try:
			root = self.output_root.resolve(strict=True)
		except (OSError, RuntimeError):
			return []
		found: Dict[Path, OpenRunSummary] = {}
		visited = set()
		stack = [(root, root, 0)]   # (path as reached, its real path, depth)
		while stack:
			reached, real, depth = stack.pop()
			if real in visited:
				continue
			visited.add(real)
			if (real / BUNDLE_DIR / MANIFEST_NAME).is_file():
				summary = self._summary(root, reached, real)
				if summary is not None:
					found.setdefault(real, summary)
				continue   # a result's own folders (images, logs, the bundle) hold no further results
			if depth >= self.max_depth:
				continue
			try:
				entries = sorted(os.scandir(str(real)), key=lambda entry: entry.name)
			except OSError:
				continue
			for entry in reversed(entries):
				if entry.name.startswith(".") or entry.name == BUNDLE_DIR:
					continue
				try:
					if not entry.is_dir(follow_symlinks=True):
						continue
					child_real = Path(entry.path).resolve(strict=True)
				except (OSError, RuntimeError):
					continue
				stack.append((reached / entry.name, child_real, depth + 1))
		return sorted(found.values(), key=lambda item: (-item.modified, str(item.canonical_path)))

	def _summary(self, root: Path, reached: Path, real: Path) -> Optional[OpenRunSummary]:
		bundle = bundle_root_of(real)
		try:
			manifest = validate_manifest(bundle, check_members=False)
			modified = (bundle / MANIFEST_NAME).stat().st_mtime
		except (ManifestError, OSError):
			return None
		try:
			relative = real.relative_to(root).as_posix()
		except ValueError:
			relative = reached.relative_to(root).as_posix()
		dataset, timebase = manifest["dataset"], manifest["timebase"]
		return OpenRunSummary(
			canonical_path=real,
			relative_path=relative,
			title=str(manifest.get("title") or real.name),
			dataset_kind=dataset["kind"],
			sequence=dataset["sequence"],
			snapshot_count=manifest["snapshots"]["count"],
			time_first=timebase["first"],
			time_last=timebase["last"],
			created_at=manifest.get("created_at"),
			modified=modified,
		)
