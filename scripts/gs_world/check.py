"""Check the gsplat install: compile the CUDA extension (first run only) and render and backpropagate a tiny scene.

	docker compose run --rm -T daaam-ws python -m scripts.gs_world.check     (just gs-check)

The first run compiles gsplat for the installed GPU (several minutes, MAX_JOBS=4 parallel nvcc jobs); later runs load
the cached extension in a few seconds.
"""

import os
import sys
import time

from . import env


def main() -> int:
	gsplat = env.import_gsplat()
	import torch

	print(f"gsplat {gsplat.__version__} from {env.GS_SITE}; torch {torch.__version__}, CUDA {torch.version.cuda}")
	if not torch.cuda.is_available():
		print("CUDA is not available in this container", file=sys.stderr)
		return 1
	major, minor = torch.cuda.get_device_capability(0)
	print(f"GPU {torch.cuda.get_device_name(0)} (sm_{major}{minor}); TORCH_CUDA_ARCH_LIST={os.environ.get('TORCH_CUDA_ARCH_LIST')}, "
		f"MAX_JOBS={os.environ.get('MAX_JOBS')}, TORCH_EXTENSIONS_DIR={os.environ.get('TORCH_EXTENSIONS_DIR')}")

	tic = time.time()
	from gsplat.cuda._backend import _C

	if _C is None:
		print("the gsplat CUDA extension is not available (no CUDA toolkit found?)", file=sys.stderr)
		return 1
	print(f"CUDA extension ready after {time.time() - tic:.1f} s")

	# a small cloud of Gaussians 2-4 m in front of an identity camera, rendered with the training settings
	torch.manual_seed(0)
	device = "cuda"
	n, width, height = 4000, 160, 120
	means = (torch.rand(n, 3, device=device) - 0.5) * torch.tensor([2.0, 1.5, 2.0], device=device)
	means[:, 2] += 3.0
	means.requires_grad_(True)
	quats = torch.nn.functional.normalize(torch.randn(n, 4, device=device), dim=-1).requires_grad_(True)
	scales = torch.full((n, 3), -3.5, device=device, requires_grad=True)
	opacities = torch.full((n,), 0.5, device=device, requires_grad=True)
	colors = torch.rand(n, 1, 3, device=device, requires_grad=True)
	viewmat = torch.eye(4, device=device)[None]
	K = torch.tensor([[[150.0, 0.0, width / 2], [0.0, 150.0, height / 2], [0.0, 0.0, 1.0]]], device=device)
	renders, alphas, info = gsplat.rasterization(
		means, quats, torch.exp(scales), torch.sigmoid(opacities), colors, viewmat, K, width, height,
		sh_degree=0, packed=False, render_mode="RGB+ED", rasterize_mode="antialiased")
	rgb, depth = renders[..., :3], renders[..., 3:4]
	covered_mask = alphas.detach() > 0.5
	covered = float(covered_mask.float().mean())
	loss = rgb.mean() + 0.1 * depth.mean()
	loss.backward()
	grads = [t.grad for t in (means, quats, scales, opacities, colors)]
	finite = all(g is not None and bool(torch.isfinite(g).all()) for g in grads)
	median_depth = float(depth.detach()[covered_mask].median()) if covered > 0 else float("nan")
	print(f"render {width}x{height}: {covered:.0%} of the pixels covered, median depth {median_depth:.2f} m, "
		f"gradients finite: {finite}")
	if not finite or covered < 0.2 or not (2.0 < median_depth < 4.5):
		print("gs-check FAILED: unexpected render or gradients", file=sys.stderr)
		return 1

	from gsplat.strategy import MCMCStrategy  # noqa: F401  (the relocation kernels are part of the extension)

	print("gs-check OK")
	return 0


if __name__ == "__main__":
	sys.exit(main())
