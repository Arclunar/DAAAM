#!/usr/bin/env python3
"""Download the model weights DAAAM loads at runtime, so the first pipeline run does not stall.

Usage:
	python scripts/download_models.py                      # FastSAM + DAM-3B + CLIP weights
	python scripts/download_models.py --sentence-t5        # additionally the post-processing text encoder
	python scripts/download_models.py --only dam           # a single item

What goes where:
	fastsam      checkpoints/fastsam/FastSAM-{x,s}.pt          (DAAAM never downloads these itself)
	dam          $HF_HOME/hub/models--nvidia--DAM-3B           (7.1 GB, not gated)
	clip-l14     $HF_HOME/hub/...                              (OpenCLIP ViT-L-14, OpenAI weights, select-frame features)
	clip-reid-base  checkpoints/clip/ViT-B-16.pt               (335 MiB, OpenAI CLIP weights; boxmot's CLIP-ReID tracker
	                                                            backend downloads them at every start otherwise, and
	                                                            scripts/run_pipeline.py links the file into ~/.cache/clip)
	sentence-t5  $HF_HOME/hub/models--sentence-transformers--sentence-t5-xl

Network access goes through the usual HTTP(S)_PROXY environment variables. The Docker Compose service
`daaam-ws` keeps HF_HOME inside the workspace volume, so downloads survive `docker compose run --rm`.
"""

import argparse
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
FASTSAM_URL = "https://github.com/ultralytics/assets/releases/download/v8.3.0/{name}"
# the same URL boxmot uses for "ViT-B-16"; the sha256 of the file is the second to last path component
CLIP_REID_BASE_URL = "https://openaipublic.azureedge.net/clip/models/5806e77cd80f8b59890b7e101eabd078d9fb84e6937f9e85e4ecb61988df416f/ViT-B-16.pt"


def fetch_fastsam(names=("FastSAM-x.pt", "FastSAM-s.pt")) -> None:
	out_dir = ROOT_DIR / "checkpoints" / "fastsam"
	out_dir.mkdir(parents=True, exist_ok=True)
	for name in names:
		dest = out_dir / name
		if dest.exists() and dest.stat().st_size > 1_000_000:
			print(f"[fastsam] {dest} already present ({dest.stat().st_size / 1e6:.0f} MB)")
			continue
		print(f"[fastsam] downloading {name}")
		tmp = dest.with_suffix(".part")
		urllib.request.urlretrieve(FASTSAM_URL.format(name=name), tmp)  # honours the *_proxy variables
		tmp.rename(dest)
		print(f"[fastsam] saved {dest} ({dest.stat().st_size / 1e6:.0f} MB)")


def fetch_hf_repo(label: str, repo_id: str) -> None:
	from huggingface_hub import snapshot_download

	print(f"[{label}] snapshot_download({repo_id}) into HF_HOME={os.environ.get('HF_HOME', '~/.cache/huggingface')}")
	path = snapshot_download(repo_id)
	print(f"[{label}] ready at {path}")


def fetch_clip_l14() -> None:
	import open_clip

	print("[clip-l14] open_clip ViT-L-14 / openai")
	open_clip.create_model_and_transforms("ViT-L-14", pretrained="openai", device="cpu")
	print("[clip-l14] ready")


def sha256_of(path: Path) -> str:
	digest = hashlib.sha256()
	with open(path, "rb") as f:
		for chunk in iter(lambda: f.read(1 << 20), b""):
			digest.update(chunk)
	return digest.hexdigest()


def fetch_clip_reid_base() -> None:
	dest = ROOT_DIR / "checkpoints" / "clip" / "ViT-B-16.pt"
	expected = CLIP_REID_BASE_URL.split("/")[-2]
	if dest.is_file() and sha256_of(dest) == expected:
		print(f"[clip-reid-base] {dest} already present ({dest.stat().st_size / 1e6:.0f} MB, sha256 ok)")
		return
	dest.parent.mkdir(parents=True, exist_ok=True)
	print("[clip-reid-base] downloading ViT-B-16.pt")
	tmp = dest.with_suffix(".part")
	urllib.request.urlretrieve(CLIP_REID_BASE_URL, tmp)  # honours the *_proxy variables
	if sha256_of(tmp) != expected:
		tmp.unlink()
		raise RuntimeError("sha256 mismatch for the downloaded ViT-B-16.pt")
	tmp.rename(dest)
	print(f"[clip-reid-base] saved {dest} ({dest.stat().st_size / 1e6:.0f} MB)")


def main() -> int:
	parser = argparse.ArgumentParser(description="Download DAAAM model weights")
	parser.add_argument("--only", choices=["fastsam", "dam", "clip-l14", "clip-reid-base", "sentence-t5"], help="Download just this item")
	parser.add_argument("--sentence-t5", action="store_true", help="Also download sentence-t5-xl (used by postprocess_scene_graph.py)")
	args = parser.parse_args()

	items = [args.only] if args.only else ["fastsam", "dam", "clip-l14", "clip-reid-base"]
	if args.sentence_t5 and "sentence-t5" not in items and not args.only:
		items.append("sentence-t5")

	failed = []
	for item in items:
		try:
			if item == "fastsam":
				fetch_fastsam()
			elif item == "dam":
				fetch_hf_repo("dam", "nvidia/DAM-3B")
			elif item == "clip-l14":
				fetch_clip_l14()
			elif item == "clip-reid-base":
				fetch_clip_reid_base()
			elif item == "sentence-t5":
				fetch_hf_repo("sentence-t5", "sentence-transformers/sentence-t5-xl")
		except Exception as e:  # keep going, report at the end
			print(f"[{item}] FAILED: {type(e).__name__}: {e}", file=sys.stderr)
			failed.append(item)

	print("done" if not failed else f"failed: {', '.join(failed)}")
	return 1 if failed else 0


if __name__ == "__main__":
	sys.exit(main())
