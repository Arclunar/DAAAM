#!/usr/bin/env python3
"""Check that this environment can run the offline DAAAM pipeline: GPU, Python packages and model files.

Usage (inside the container, from the repository root):
	python scripts/check_deployment.py              # files and imports only
	python scripts/check_deployment.py --load-dam   # additionally load DAM-3B onto the GPU the way the pipeline does

Prints one line per check (OK / WARN / FAIL). The exit code is 1 when something the pipeline needs is missing;
warnings (a small GPU, optional packages and models) do not fail the check. --load-dam is the real test that the
pipeline can start without internet access: the grounding worker silently drops out when DAM cannot be loaded.
"""

import argparse
import importlib
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
MIN_GPU_GIB = 15.0  # DAM-3B inference peaked at 15.9 GB on the 16 GB card used for the reference run

failures = 0


def report(level: str, what: str, detail: str = "") -> None:
	global failures
	if level == "FAIL":
		failures += 1
	print(f"  {level:4s} {what:34s} {detail}")


def check_gpu() -> None:
	print("GPU")
	try:
		import torch
	except Exception as e:
		report("FAIL", "torch", f"{type(e).__name__}: {e}")
		return
	if not torch.cuda.is_available():
		report("FAIL", "torch.cuda.is_available()", "False (is the container started with --gpus all and a driver >= 570?)")
		return
	props = torch.cuda.get_device_properties(0)
	gib = props.total_memory / 2**30
	report("OK", "device", f"{props.name}, {gib:.1f} GiB, capability {props.major}.{props.minor}, torch {torch.__version__} / CUDA {torch.version.cuda}")
	try:
		value = (torch.ones(8, device="cuda") * 2).sum().item()
		assert value == 16.0
		report("OK", "CUDA kernel", "ran a test kernel")
	except Exception as e:
		report("FAIL", "CUDA kernel", f"{type(e).__name__}: {e}")
	if gib < MIN_GPU_GIB:
		report("WARN", "GPU memory", f"{gib:.1f} GiB; DAM-3B inference needs about {MIN_GPU_GIB:.0f} GiB, reduce the number of grounding workers or use a larger GPU")


def check_imports() -> None:
	print("Python packages")
	required = ["hydra_python", "spark_dsg", "daaam", "boxmot", "dam", "ultralytics", "open_clip", "cv2", "cvxpy", "gradio"]
	optional = ["sentence_transformers", "rerun", "tensorrt"]
	for names, level in ((required, "FAIL"), (optional, "WARN")):
		for name in names:
			try:
				module = importlib.import_module(name)
				report("OK", name, str(getattr(module, "__version__", "")))
			except Exception as e:
				report(level, name, f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")
	try:
		import cvxpy

		if "GLPK_MI" in cvxpy.installed_solvers():
			report("OK", "cvxpy GLPK_MI solver", "")
		else:
			report("WARN", "cvxpy GLPK_MI solver", "missing; the assignment worker falls back to a 5-7x slower solver (pip install cvxopt)")
	except Exception:
		pass


def check_files() -> None:
	print("Model files")
	for rel in ("checkpoints/fastsam/FastSAM-x.pt", "checkpoints/reid_weights/clip_general.pt", "checkpoints/clip/ViT-B-16.pt"):
		path = ROOT_DIR / rel
		if path.is_file():
			report("OK", rel, f"{path.stat().st_size / 1e6:.0f} MB")
		else:
			report("FAIL", rel, "missing (python scripts/download_models.py)")

	try:
		from huggingface_hub import snapshot_download
	except Exception as e:
		report("FAIL", "huggingface_hub", f"{type(e).__name__}: {e}")
		return
	for repo, required in (("nvidia/DAM-3B", True), ("timm/vit_large_patch14_clip_224.openai", True),
	                       ("sentence-transformers/sentence-t5-xl", False)):
		try:
			path = Path(snapshot_download(repo, local_files_only=True))
			weights = [p for p in path.rglob("*") if p.suffix in (".safetensors", ".bin", ".pt", ".pth") and p.exists()]
			if weights:
				report("OK", f"HF cache: {repo}", f"{len(weights)} weight file(s)")
			elif required:
				report("FAIL", f"HF cache: {repo}", "snapshot found but it contains no weight files")
			else:
				report("WARN", f"HF cache: {repo}", "snapshot found but it contains no weight files")
		except Exception:
			if required:
				report("FAIL", f"HF cache: {repo}", "not in the cache (python scripts/download_models.py)")
			else:
				report("WARN", f"HF cache: {repo}", "not in the cache (only needed by scripts/postprocess_scene_graph.py)")


def check_dam_load() -> None:
	print("DAM-3B (loading it onto the GPU)")
	try:
		from daaam.query_manager.dam import DAMAgentPanoptic
		from daaam.utils.hf_cache import resolve_cached_hf_model

		path = resolve_cached_hf_model("nvidia/DAM-3B")
		start = time.time()
		DAMAgentPanoptic(model_path=path, conv_mode="v1", prompt_mode="focal_prompt")
		report("OK", "DAM-3B load", f"{time.time() - start:.0f} s from {path}")
	except Exception as e:
		report("FAIL", "DAM-3B load", f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")


def main() -> int:
	parser = argparse.ArgumentParser(description="Check the DAAAM deployment")
	parser.add_argument("--load-dam", action="store_true", help="also load DAM-3B (about 7 GB of GPU memory)")
	args = parser.parse_args()

	check_gpu()
	check_imports()
	check_files()
	if args.load_dam:
		check_dam_load()
	print("\nenvironment is ready" if not failures else f"\n{failures} check(s) failed")
	return 1 if failures else 0


if __name__ == "__main__":
	sys.exit(main())
