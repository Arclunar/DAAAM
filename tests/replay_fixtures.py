"""Small valid replay bundles for the service tests (standard library only, so they run on the WSL host).

The timeline chunks hold placeholder payloads: enough for the manifest, path and HTTP contracts. Tests that render a
replay build their bundles with scripts/replay_bundle.finalize_bundle instead.
"""

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any, Dict

from scripts.replay_web.manifest import GRAPH_CANDIDATES, SCHEMA_VERSION

JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb00430006040506050406060506070706080a100a0a09090a140e0f0c1017141818171416161a1d251f1a1b231c1616202c20232627292a29191f2d302d283025282928ffdb0043010707070a080a130a0a13281a161a2828282828282828282828282828282828282828282828282828282828282828282828282828282828282828282828282828ffc00011080002000203012200021101031101ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffc4001f0100030101010101010101010000000000000102030405060708090a0bffc400b51100020102040403040705040400010277000102031104052131061241510761711322328108144291a1b1c109233352f0156272d10a162434e125f11718191a262728292a35363738393a434445464748494a535455565758595a636465666768696a737475767778797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9faffda000c03010002110311003f008e8a28af883c23ffd9")


def write_chunk(path: Path, records) -> Dict[str, Any]:
	body = json.dumps({"snapshots": records}, separators=(",", ":")).encode("utf-8")
	payload = gzip.compress(body, mtime=0)
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_bytes(payload)
	return {"bytes": len(payload), "uncompressed_bytes": len(body), "sha256": hashlib.sha256(payload).hexdigest()}


def write_minimal_bundle(result: Path, dataset_kind: str = "tum", sequence: str = "fr1_desk", title: str = "fixture",
		snapshots: int = 3, description_frame: bool = True) -> Path:
	"""A finalized bundle at RESULT/replay with one chunk per snapshot, a JPEG per snapshot and one source frame."""
	bundle = Path(result) / "replay"
	chunks, timeline = [], []
	for index in range(snapshots):
		t = round(0.1 * (index + 1), 3)
		relative = "timeline/%06d.json.gz" % index
		chunks.append({
			"index": index, "path": relative, "first_snapshot": index, "snapshot_count": 1, "t_first": t, "t_last": t,
			**write_chunk(bundle / relative, [{"n": index + 1, "t": t, "p": ""}]),
		})
		for camera in ("rgb", "depth"):
			image = bundle / "images" / "timeline" / camera / ("%06d.jpg" % index)
			image.parent.mkdir(parents=True, exist_ok=True)
			image.write_bytes(JPEG)
		timeline.append({"index": index, "rgb": "images/timeline/rgb/%06d.jpg" % index, "depth": "images/timeline/depth/%06d.jpg" % index})
	descriptions = {}
	if description_frame:
		(bundle / "images" / "descriptions").mkdir(parents=True, exist_ok=True)
		(bundle / "images" / "descriptions" / "000001.jpg").write_bytes(JPEG)
		descriptions["2"] = {"frame_id": 2, "timestamp": 0.133, "camera": "rgb", "index": 1, "path": "images/descriptions/000001.jpg"}
	times = [chunk["t_first"] for chunk in chunks]
	manifest = {
		"schema_version": SCHEMA_VERSION,
		"generator": {"name": "tests/replay_fixtures.py", "version": 1},
		"created_at": "2026-10-17T00:00:00Z",
		"finalized": True,
		"title": title,
		"dataset": {
			"kind": dataset_kind, "sequence": sequence,
			"time_origin": 0.0 if dataset_kind == "tum" else 1673884185.589118,
			"fps": 15.0 if dataset_kind == "tum" else 10.0, "start_frame": 0, "frame_count": None, "source": None,
		},
		"timebase": {"unit": "s", "origin": "dataset.time_origin", "first": times[0], "last": times[-1]},
		"coordinates": {"frame": "world", "up": "z"},
		"quant": {"min": [0.0, 0.0, 0.0], "scale": [1e-4, 1e-4, 1e-4]},
		"snapshots": {"count": snapshots, "numbers": list(range(1, snapshots + 1)), "times": times},
		"chunks": chunks,
		"layers": ["objects"],
		"cameras": [{"key": "rgb", "label": "RGB"}, {"key": "depth", "label": "深度", "max_depth": 4.0}],
		"images": {"frame_total": snapshots, "timeline": timeline, "descriptions": descriptions},
		"labels": {"42": "a green chair"},
		"description_sources": {"42": {"frame_id": 2, "timestamp": 0.133}} if description_frame else {},
		"catalog": {"objects": [], "places": []},
		"reference_mesh": None,
		"graph_candidates": list(GRAPH_CANDIDATES),
		"build": {},
	}
	(bundle / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
	return Path(result)
