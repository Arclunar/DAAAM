"""Image and depth metrics (torch): PSNR, SSIM (the 3DGS 11x11 Gaussian window), colour-corrected PSNR, depth errors."""

from typing import Dict, Optional

import torch
import torch.nn.functional as F

_WINDOWS: Dict[tuple, torch.Tensor] = {}


def _window(channels: int, device, dtype, size: int = 11, sigma: float = 1.5) -> torch.Tensor:
	key = (channels, str(device), dtype, size, sigma)
	if key not in _WINDOWS:
		coords = torch.arange(size, dtype=dtype, device=device) - size // 2
		g = torch.exp(-(coords ** 2) / (2 * sigma ** 2))
		g = g / g.sum()
		_WINDOWS[key] = (g[:, None] @ g[None, :]).expand(channels, 1, size, size).contiguous()
	return _WINDOWS[key]


def ssim_map(a: torch.Tensor, b: torch.Tensor) -> torch.Tensor:
	"""Per-pixel SSIM of two images [H, W, C] with values in [0, 1], averaged over the channels -> [H, W]."""
	x = a.permute(2, 0, 1)[None]
	y = b.permute(2, 0, 1)[None]
	channels = x.shape[1]
	w = _window(channels, x.device, x.dtype)
	pad = w.shape[-1] // 2
	mu_x = F.conv2d(x, w, padding=pad, groups=channels)
	mu_y = F.conv2d(y, w, padding=pad, groups=channels)
	sigma_x = F.conv2d(x * x, w, padding=pad, groups=channels) - mu_x ** 2
	sigma_y = F.conv2d(y * y, w, padding=pad, groups=channels) - mu_y ** 2
	sigma_xy = F.conv2d(x * y, w, padding=pad, groups=channels) - mu_x * mu_y
	c1, c2 = 0.01 ** 2, 0.03 ** 2
	ssim = ((2 * mu_x * mu_y + c1) * (2 * sigma_xy + c2)) / ((mu_x ** 2 + mu_y ** 2 + c1) * (sigma_x + sigma_y + c2))
	return ssim[0].mean(0)


def masked_mean(x: torch.Tensor, mask: Optional[torch.Tensor]) -> torch.Tensor:
	"""Mean of x [H, W(, C)] over the pixels where mask [H, W] is true (all pixels when mask is None)."""
	return x.mean() if mask is None else x[mask].mean()


def psnr(a: torch.Tensor, b: torch.Tensor, mask: Optional[torch.Tensor] = None) -> float:
	"""PSNR in dB of images [H, W, C] in [0, 1] over the masked pixels."""
	mse = masked_mean((a - b) ** 2, mask)
	return float(-10.0 * torch.log10(mse.clamp_min(1e-10)))


def color_correct(render: torch.Tensor, target: torch.Tensor, mask: Optional[torch.Tensor] = None) -> torch.Tensor:
	"""render mapped by the affine colour transform (3x4, least squares) that best matches target on the mask.

	Removes exposure and white-balance differences so the remaining error measures geometry and texture.
	"""
	src = render[mask] if mask is not None else render.reshape(-1, 3)
	dst = target[mask] if mask is not None else target.reshape(-1, 3)
	ones = torch.ones_like(src[:, :1])
	A = torch.cat([src, ones], dim=1).double()
	solution = torch.linalg.lstsq(A, dst.double()).solution  # [4, 3]
	full = torch.cat([render.reshape(-1, 3), torch.ones_like(render.reshape(-1, 3)[:, :1])], dim=1).double()
	return (full @ solution).float().reshape(render.shape)


def depth_metrics(pred: torch.Tensor, target: torch.Tensor, mask: torch.Tensor) -> Dict[str, float]:
	"""Errors of rendered depth against measured depth (metres) on mask & target > 0 & pred > 0."""
	valid = mask & (target > 0) & (pred > 0)
	if int(valid.sum()) == 0:
		return {"depth_mae_m": float("nan"), "depth_rmse_m": float("nan"), "depth_abs_rel": float("nan"),
			"depth_delta1": float("nan"), "depth_pixels": 0}
	p, t = pred[valid].double(), target[valid].double()
	ratio = torch.maximum(p / t, t / p)
	return {
		"depth_mae_m": float((p - t).abs().mean()),
		"depth_rmse_m": float(torch.sqrt(((p - t) ** 2).mean())),
		"depth_abs_rel": float(((p - t).abs() / t).mean()),
		"depth_delta1": float((ratio < 1.25).double().mean()),
		"depth_pixels": int(valid.sum()),
	}
