"""Locate the isolated gsplat install and prepare its one-time JIT build of the CUDA extension.

`just gs-install` puts gsplat and its three small dependencies into GS_SITE (default /opt/daaam_ws/gs_site, inside the
workspace volume) with `pip install --no-deps --target`, so the DAAAM virtual environment stays untouched. The CUDA
extension is compiled by torch the first time gsplat is used and kept in TORCH_EXTENSIONS_DIR (also in the volume).
"""

import os
import sys
from pathlib import Path

GS_SITE = Path(os.environ.get("GS_SITE", "/opt/daaam_ws/gs_site"))
GSPLAT_VERSION = "1.5.3"
# pinned by `just gs-install`; gsplat's other requirements (numpy, torch, rich) already are in the DAAAM environment
PACKAGES = ("gsplat==1.5.3", "ninja==1.13.2", "jaxtyping==0.3.11", "wadler-lindig==0.1.7")


def activate() -> Path:
	"""Make the isolated install importable and set the build environment (idempotent)."""
	site = GS_SITE
	if site.is_dir() and str(site) not in sys.path:
		sys.path.insert(0, str(site))
	bin_dir = site / "bin"  # torch's JIT build calls `ninja` from PATH
	path = os.environ.get("PATH", "")
	if bin_dir.is_dir() and str(bin_dir) not in path.split(os.pathsep):
		os.environ["PATH"] = str(bin_dir) + os.pathsep + path
	# every nvcc job of the build takes 1-2 GB of RAM; gsplat would start 10
	os.environ.setdefault("MAX_JOBS", "4")
	os.environ.setdefault("TORCH_EXTENSIONS_DIR", "/opt/daaam_ws/cache/torch_extensions")
	if "TORCH_CUDA_ARCH_LIST" not in os.environ:
		# compile for the installed GPU only (sm_120 on the RTX 5060 Ti) instead of every architecture torch knows
		try:
			import torch

			if torch.cuda.is_available():
				major, minor = torch.cuda.get_device_capability(0)
				os.environ["TORCH_CUDA_ARCH_LIST"] = f"{major}.{minor}"
		except Exception:
			pass
	return site


def import_gsplat():
	"""Import gsplat from the isolated install, with a clear message when it is missing."""
	activate()
	try:
		import gsplat
	except ImportError as exc:
		raise SystemExit(f"gsplat is not installed in {GS_SITE} ({exc}); run `just gs-install` first") from exc
	if getattr(gsplat, "__version__", None) != GSPLAT_VERSION:
		print(f"warning: gsplat {getattr(gsplat, '__version__', '?')} found, gs_world is tested with {GSPLAT_VERSION}",
			file=sys.stderr)
	return gsplat
