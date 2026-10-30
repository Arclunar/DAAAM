"""Whether a first description is untrustworthy (E): strategies over q', the mean token probability p and their joint score."""
import numpy as np

REASONS = {  # code -> (Chinese for viewers, English for the fusion prompt)
	'low_view_quality': ('视角质量低', 'low view quality'),
	'low_confidence': ('模型把握低', 'low model confidence'),
	'low_joint_score': ('视角质量与模型把握综合分低', 'low combined view quality and model confidence'),
	'small_area': ('物体在画面中过小', 'object too small in the image'),
	'truncated': ('物体被画面边缘截断', 'object cut off by the image border'),
}


def _below(value, key, seen, fixed, cfg):
	"""value < threshold; quantile thresholds are causal: over every value seen so far including this one."""
	if value is None:
		return False
	seen[key].append(value)
	tau = fixed if cfg.threshold_mode == 'fixed' else float(np.quantile(seen[key], cfg.threshold_quantile))
	return value < tau


def decide(q_prime, p, area_ratio, truncated, cfg, seen):
	"""Reason codes of a trigger (empty: trusted). seen holds the q', p and joint values of earlier objects."""
	low_q = _below(q_prime, 'q', seen, cfg.tau_q, cfg) if cfg.trigger in ('q', 'or') else False
	low_p = _below(p, 'p', seen, cfg.tau_p, cfg) if cfg.trigger in ('gamma', 'or') else False
	joint = None if p is None else max(q_prime, 0.0) ** cfg.beta * max(p, 0.0) ** (1 - cfg.beta)
	low_joint = _below(joint, 'joint', seen, cfg.tau_s, cfg) if cfg.trigger == 'joint' else False
	reasons = [code for code, hit in (('low_view_quality', low_q), ('low_confidence', low_p), ('low_joint_score', low_joint)) if hit]
	if cfg.trigger != 'none' and cfg.trigger_area:
		reasons += [code for code, hit in (('small_area', area_ratio < cfg.area_min), ('truncated', truncated)) if hit]
	return reasons
