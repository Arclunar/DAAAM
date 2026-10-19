"""The versioned replay bundle: RESULT/replay/manifest.json and the files it names.

A mapping result can be opened in the replay workspace only when its `replay/manifest.json` is valid. The finalizer
(scripts/replay_bundle.py) writes every other bundle file first and the manifest last, through a temporary sibling and
an atomic rename, so a manifest is the commit point of a complete bundle. Every path inside the manifest is a
normalized relative path that must stay inside the bundle directory, symlinks included; the service resolves files
only through `resolve_bundle_member`, never from a path chosen by the browser.

Standard library only, Python 3.8: the WSL control service imports this module.
"""

import hashlib
import json
import math
import os
import re
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Mapping, Optional

SCHEMA_VERSION = 1
BUNDLE_DIR = "replay"
MANIFEST_NAME = "manifest.json"
DATASET_KINDS = frozenset({"tum", "coda"})
GRAPH_CANDIDATES = (
	"clustered_dsg_with_summaries.json",
	"clustered_dsg.json",
	"dsg_updated.json",
	"dsg.json",
)
REQUIRED_KEYS = (
	"schema_version",
	"finalized",
	"dataset",
	"timebase",
	"quant",
	"snapshots",
	"chunks",
	"cameras",
	"images",
	"graph_candidates",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(ValueError):
	"""A bundle that the workspace must not open; the message names the file and what is wrong."""


def bundle_root_of(result_dir: Path) -> Path:
	"""The replay bundle directory of a mapping result directory."""
	return Path(result_dir) / BUNDLE_DIR


def member_name(relative: Any) -> PurePosixPath:
	"""`relative` as a bundle member name, or ManifestError: a normalized POSIX path below the bundle (no absolute path,
	drive, backslash, `.` or `..` part, empty or doubled separator)."""
	if not isinstance(relative, str) or not relative or "\x00" in relative or "\\" in relative or ":" in relative:
		raise ManifestError(f"unsafe bundle member: {relative!r}")
	member = PurePosixPath(relative)
	if member.is_absolute() or not member.parts or ".." in member.parts or member.as_posix() != relative:
		raise ManifestError(f"unsafe bundle member: {relative!r}")
	return member


def resolve_bundle_member(bundle_root: Path, relative: str) -> Path:
	"""The real path of an existing bundle member, or ManifestError.

	`relative` must be a valid member name (member_name), and the file it resolves to, symlinks followed, must still lie
	inside the bundle."""
	member = member_name(relative)
	try:
		root = Path(bundle_root).resolve(strict=True)
	except (OSError, RuntimeError) as error:
		raise ManifestError(f"bundle directory {bundle_root} is not accessible ({error})") from error
	try:
		resolved = (root / member).resolve(strict=True)
	except FileNotFoundError as error:
		raise ManifestError(f"missing bundle member {relative} in {root}") from error
	except (OSError, RuntimeError) as error:   # RuntimeError: a symlink loop (Python 3.8)
		raise ManifestError(f"bundle member {relative} in {root} cannot be resolved ({error})") from error
	if resolved != root and root not in resolved.parents:
		raise ManifestError(f"bundle member leaves {root}: {relative}")
	return resolved


def write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
	"""Write JSON so that readers see the old file or the complete new one, never a partial file."""
	path = Path(path)
	temporary = path.with_name(f".{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
	try:
		with temporary.open("w", encoding="utf-8") as stream:
			json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
			stream.flush()
			os.fsync(stream.fileno())
		os.replace(str(temporary), str(path))
	finally:
		if temporary.exists():
			temporary.unlink()


def file_sha256(path: Path, block: int = 1 << 20) -> str:
	digest = hashlib.sha256()
	with Path(path).open("rb") as stream:
		for data in iter(lambda: stream.read(block), b""):
			digest.update(data)
	return digest.hexdigest()


def load_manifest_json(bundle_root: Path) -> Dict[str, Any]:
	"""The parsed manifest object, unvalidated; ManifestError names the expected path when it is missing or broken."""
	path = Path(bundle_root) / MANIFEST_NAME
	expected = f"{path} (replay bundle manifest, schema {SCHEMA_VERSION})"
	try:
		text = path.read_text(encoding="utf-8")
	except FileNotFoundError as error:
		raise ManifestError(f"no finalized replay bundle: expected {expected}") from error
	except (OSError, UnicodeDecodeError) as error:
		raise ManifestError(f"cannot read {expected}: {error}") from error
	try:
		value = json.loads(text)
	except ValueError as error:
		raise ManifestError(f"{path} is not valid JSON ({error}); expected schema {SCHEMA_VERSION}") from error
	if not isinstance(value, dict):
		raise ManifestError(f"{path} must contain a JSON object (replay bundle manifest, schema {SCHEMA_VERSION})")
	return value


def validate_manifest(bundle_root: Path, verify_digests: bool = False, check_members: bool = True) -> Dict[str, Any]:
	"""The manifest of a finalized bundle after checking its schema and every file it names.

	Sizes and containment of all members are checked unless `check_members` is false (listing many results checks the
	schema and member names only; opening one checks everything); `verify_digests` also re-hashes every chunk (the
	finalizer does this once after writing; opening a large result for browsing does not)."""
	manifest = load_manifest_json(bundle_root)
	_Validator(Path(bundle_root), manifest, verify_digests, check_members).run()
	return manifest


def _is_int(value: Any) -> bool:
	return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
	return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class _Validator:
	def __init__(self, root: Path, manifest: Dict[str, Any], verify_digests: bool, check_members: bool):
		self.root = root
		self.path = root / MANIFEST_NAME
		self.manifest = manifest
		self.verify_digests = verify_digests
		self.check_members = check_members

	def fail(self, message: str) -> None:
		raise ManifestError(f"{self.path}: {message} (replay bundle schema {SCHEMA_VERSION})")

	def member(self, relative: Any, field: str) -> Optional[Path]:
		if not isinstance(relative, str):
			self.fail(f"{field} must be a relative path string")
		try:
			if not self.check_members:
				member_name(relative)
				return None
			path = resolve_bundle_member(self.root, relative)
		except ManifestError as error:
			self.fail(f"{field}: {error}")
		if not path.is_file():
			self.fail(f"{field}: {relative} is not a file")
		return path

	def mapping(self, value: Any, field: str) -> Dict[str, Any]:
		if not isinstance(value, dict):
			self.fail(f"{field} must be an object")
		return value

	def run(self) -> None:
		m = self.manifest
		for key in REQUIRED_KEYS:
			if key not in m:
				self.fail(f"missing required key '{key}'")
		if not _is_int(m["schema_version"]) or m["schema_version"] != SCHEMA_VERSION:
			self.fail(f"unsupported schema_version {m['schema_version']!r}; this workspace reads schema {SCHEMA_VERSION} only")
		if m["finalized"] is not True:
			self.fail("the bundle is not finalized (finalized must be true)")
		self.dataset(self.mapping(m["dataset"], "dataset"))
		self.timebase(self.mapping(m["timebase"], "timebase"))
		self.quant(self.mapping(m["quant"], "quant"))
		count = self.snapshots(self.mapping(m["snapshots"], "snapshots"))
		self.chunks(m["chunks"], count)
		cameras = self.cameras(m["cameras"])
		self.images(self.mapping(m["images"], "images"), cameras, count)
		self.graph_candidates(m["graph_candidates"])
		for key in ("labels", "description_sources", "catalog", "build", "generator", "coordinates"):
			if key in m and m[key] is not None:
				self.mapping(m[key], key)
		if m.get("reference_mesh") is not None:
			reference = self.mapping(m["reference_mesh"], "reference_mesh")
			self.member(reference.get("path"), "reference_mesh.path")
			for key in ("vertices", "triangles"):
				if not _is_int(reference.get(key)) or reference[key] < 0:
					self.fail(f"reference_mesh.{key} must be a non-negative integer")

	def dataset(self, dataset: Dict[str, Any]) -> None:
		if dataset.get("kind") not in DATASET_KINDS:
			self.fail(f"dataset.kind must be one of {', '.join(sorted(DATASET_KINDS))}, got {dataset.get('kind')!r}")
		if not isinstance(dataset.get("sequence"), str) or not dataset["sequence"]:
			self.fail("dataset.sequence must be a non-empty string")
		if not _is_number(dataset.get("time_origin")):
			self.fail("dataset.time_origin must be a finite number (seconds)")
		if not _is_number(dataset.get("fps")) or dataset["fps"] <= 0:
			self.fail("dataset.fps must be a positive number")
		if not _is_int(dataset.get("start_frame")) or dataset["start_frame"] < 0:
			self.fail("dataset.start_frame must be a non-negative integer")
		if "frame_count" not in dataset:
			self.fail("dataset.frame_count is required (null when the run length is not recorded)")
		if dataset["frame_count"] is not None and (not _is_int(dataset["frame_count"]) or dataset["frame_count"] < 1):
			self.fail("dataset.frame_count must be null or a positive integer")

	def timebase(self, timebase: Dict[str, Any]) -> None:
		if not _is_number(timebase.get("first")) or not _is_number(timebase.get("last")) or timebase["first"] > timebase["last"]:
			self.fail("timebase.first and timebase.last must be finite numbers with first <= last")

	def quant(self, quant: Dict[str, Any]) -> None:
		for key in ("min", "scale"):
			values = quant.get(key)
			if not isinstance(values, list) or len(values) != 3 or not all(_is_number(v) for v in values):
				self.fail(f"quant.{key} must be three finite numbers")
		if any(v <= 0 for v in quant["scale"]):
			self.fail("quant.scale must be positive")

	def snapshots(self, snapshots: Dict[str, Any]) -> int:
		count = snapshots.get("count")
		if not _is_int(count) or count < 1:
			self.fail("snapshots.count must be a positive integer")
		numbers, times = snapshots.get("numbers"), snapshots.get("times")
		if not isinstance(numbers, list) or len(numbers) != count or not all(_is_int(n) for n in numbers):
			self.fail("snapshots.numbers must list one integer per snapshot")
		if not isinstance(times, list) or len(times) != count or not all(_is_number(t) for t in times):
			self.fail("snapshots.times must list one finite time per snapshot")
		if any(b < a for a, b in zip(times, times[1:])):
			self.fail("snapshots.times must not decrease")
		return count

	def chunks(self, chunks: Any, count: int) -> None:
		if not isinstance(chunks, list) or not chunks:
			self.fail("chunks must be a non-empty list")
		expected_first = 0
		for index, chunk in enumerate(chunks):
			field = f"chunks[{index}]"
			chunk = self.mapping(chunk, field)
			if "index" in chunk and chunk["index"] != index:
				self.fail(f"{field}.index must be {index}")
			path = self.member(chunk.get("path"), f"{field}.path")
			if chunk.get("first_snapshot") != expected_first or not _is_int(chunk.get("first_snapshot")):
				self.fail(f"{field}.first_snapshot must be {expected_first} (chunks cover the snapshots in order)")
			if not _is_int(chunk.get("snapshot_count")) or chunk["snapshot_count"] < 1:
				self.fail(f"{field}.snapshot_count must be a positive integer")
			expected_first += chunk["snapshot_count"]
			if not _is_number(chunk.get("t_first")) or not _is_number(chunk.get("t_last")) or chunk["t_first"] > chunk["t_last"]:
				self.fail(f"{field}.t_first and t_last must be finite numbers with t_first <= t_last")
			for key in ("bytes", "uncompressed_bytes"):
				if not _is_int(chunk.get(key)) or chunk[key] < 0:
					self.fail(f"{field}.{key} must be a non-negative integer")
			if path is not None and path.stat().st_size != chunk["bytes"]:
				self.fail(f"{field}.bytes is {chunk['bytes']} but {chunk['path']} has {path.stat().st_size} bytes")
			digest = chunk.get("sha256")
			if not isinstance(digest, str) or not _SHA256.match(digest):
				self.fail(f"{field}.sha256 must be 64 lowercase hexadecimal digits")
			if self.verify_digests and path is not None and file_sha256(path) != digest:
				self.fail(f"{field}: {chunk['path']} does not match its sha256")
		if expected_first != count:
			self.fail(f"the chunks cover {expected_first} snapshots but snapshots.count is {count}")

	def cameras(self, cameras: Any) -> Iterable[str]:
		if not isinstance(cameras, list):
			self.fail("cameras must be a list")
		keys = []
		for index, camera in enumerate(cameras):
			camera = self.mapping(camera, f"cameras[{index}]")
			if not isinstance(camera.get("key"), str) or not re.match(r"^[a-z][a-z0-9_]*$", camera["key"]):
				self.fail(f"cameras[{index}].key must be a lowercase identifier")
			keys.append(camera["key"])
		if len(set(keys)) != len(keys):
			self.fail("cameras must have distinct keys")
		return keys

	def images(self, images: Dict[str, Any], cameras: Iterable[str], count: int) -> None:
		cameras = list(cameras)
		timeline = images.get("timeline")
		if not isinstance(timeline, list):
			self.fail("images.timeline must be a list")
		if cameras and len(timeline) != count:
			self.fail(f"images.timeline must have one entry per snapshot ({count}), got {len(timeline)}")
		if not cameras and timeline:
			self.fail("images.timeline must be empty when the bundle has no cameras")
		if not _is_int(images.get("frame_total")) or images["frame_total"] < 0:
			self.fail("images.frame_total must be a non-negative integer")
		for index, entry in enumerate(timeline):
			entry = self.mapping(entry, f"images.timeline[{index}]")
			if not _is_int(entry.get("index")) or entry["index"] < 0:
				self.fail(f"images.timeline[{index}].index must be a non-negative dataset frame index")
			for key in cameras:
				if entry.get(key) is not None:
					self.member(entry[key], f"images.timeline[{index}].{key}")
		descriptions = self.mapping(images.get("descriptions"), "images.descriptions")
		for key, entry in descriptions.items():
			field = f"images.descriptions[{key}]"
			entry = self.mapping(entry, field)
			if not _is_int(entry.get("frame_id")) or entry["frame_id"] < 1 or str(entry["frame_id"]) != key:
				self.fail(f"{field}.frame_id must be the positive frame ID it is keyed by")
			if entry.get("timestamp") is not None and not _is_number(entry["timestamp"]):
				self.fail(f"{field}.timestamp must be a number or null")
			if not isinstance(entry.get("camera"), str) or not entry["camera"]:
				self.fail(f"{field}.camera must name the camera")
			self.member(entry.get("path"), f"{field}.path")

	def graph_candidates(self, candidates: Any) -> None:
		if not isinstance(candidates, list) or not all(isinstance(c, str) and c in GRAPH_CANDIDATES for c in candidates):
			self.fail(f"graph_candidates must list file names from {', '.join(GRAPH_CANDIDATES)}")
		if len(set(candidates)) != len(candidates):
			self.fail("graph_candidates must not repeat a file name")


def manifest_summary(manifest: Mapping[str, Any]) -> Dict[str, Optional[Any]]:
	"""The small, browser-safe description of a bundle used in result lists."""
	dataset = manifest.get("dataset", {})
	timebase = manifest.get("timebase", {})
	return {
		"title": manifest.get("title") or "",
		"dataset_kind": dataset.get("kind"),
		"sequence": dataset.get("sequence"),
		"snapshot_count": manifest.get("snapshots", {}).get("count"),
		"time_first": timebase.get("first"),
		"time_last": timebase.get("last"),
		"created_at": manifest.get("created_at"),
	}
