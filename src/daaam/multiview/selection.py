"""View coverage on the unit sphere around an object (H) and refinement view selection strategies (I).

A view is a dict with 'direction' (unit vector, object centre -> camera, world frame) and 'q_prime'."""
import math

import numpy as np


def fibonacci(count):
	"""count near-uniform unit directions."""
	i = np.arange(count) + 0.5
	z = 1 - 2 * i / count
	r = np.sqrt(1 - z * z)
	phi = math.pi * (3 - math.sqrt(5)) * i
	return np.column_stack([r * np.cos(phi), r * np.sin(phi), z])


def angle(a, b):
	"""Angle in radians between unit vectors; a and b broadcast ((..., 3))."""
	return np.arccos(np.clip(np.sum(np.asarray(a) * np.asarray(b), axis=-1), -1.0, 1.0))


class Coverage:
	"""C(V) = mean over reachable directions m of max_{i in V} q'_i^kappa exp(-angle(b_m, d_i)^2 / 2 sigma^2). Reachable: within
	phi_reach of the initial or any candidate view, so an object seen from one side only can still be fully covered.
	Submodular, so greedy addition has the (1 - 1/e) guarantee."""

	def __init__(self, views, cfg):
		self.cfg = cfg
		directions = fibonacci(cfg.coverage_directions)
		near = angle(directions[:, None, :], np.array([v['direction'] for v in views])[None, :, :]) <= math.radians(cfg.phi_reach_deg)
		self.directions = directions[near.any(axis=1)]

	def scores(self, view):
		sigma = math.radians(self.cfg.sigma_deg)
		return max(view['q_prime'], 0.0) ** self.cfg.kappa * np.exp(-angle(self.directions, view['direction']) ** 2 / (2 * sigma * sigma))

	def __call__(self, views):
		if not len(self.directions) or not views:
			return 0.0
		return float(np.max([self.scores(v) for v in views], axis=0).mean())


def coverage_greedy(initial, candidates, cfg, rng):
	"""Initial views fixed; add the candidate with the largest coverage gain until gain < min_gain, coverage >= tau_cov or the
	budget is spent. Returns (chosen candidates, gains, coverage before, coverage after)."""
	measure = Coverage(initial + candidates, cfg)
	chosen, gains, current = [], [], measure(initial)
	before, left = current, list(candidates)
	while left and len(chosen) < cfg.refine_budget and current < cfg.tau_cov:
		values = [measure(initial + chosen + [c]) for c in left]
		best = int(np.argmax(values))
		if values[best] - current < cfg.min_gain:
			break
		gains.append(values[best] - current)
		current = values[best]
		chosen.append(left.pop(best))
	return chosen, gains, before, current


def q_max(initial, candidates, cfg, rng):
	chosen = sorted(candidates, key=lambda c: (-c['q_prime'], c['frame_id']))[:cfg.refine_budget]
	return _scored(initial, candidates, chosen, cfg)


def random_views(initial, candidates, cfg, rng):
	order = rng.permutation(len(candidates))[:cfg.refine_budget]
	return _scored(initial, candidates, [candidates[i] for i in order], cfg)


def _scored(initial, candidates, chosen, cfg):
	if any(v.get('direction') is None for v in initial + candidates):  # no geometry (no pose/depth): coverage is not defined
		return chosen, [], None, None
	measure = Coverage(initial + candidates, cfg)
	return chosen, [], measure(initial), measure(initial + chosen)


SELECTIONS = {'random': random_views, 'q_max': q_max, 'coverage_greedy': coverage_greedy}


def dedup_add(pool, item, cfg):
	"""Snapshot de-duplication: within theta_dup of an existing snapshot only the higher q' survives. Returns (kept pool,
	dropped items)."""
	near = [s for s in pool if s['view']['direction'] is not None   # kept before the track had a centre estimate
		and angle(s['view']['direction'], item['view']['direction']) < math.radians(cfg.theta_dup_deg)]
	if not near:
		return pool + [item], []
	if all(item['view']['q_prime'] > s['view']['q_prime'] for s in near):
		return [s for s in pool if all(s is not n for n in near)] + [item], near
	return pool, [item]


def evict(pool, initial, cfg):
	"""Over capacity: drop the snapshot whose removal lowers coverage the least. Returns (kept pool, dropped item)."""
	measure = Coverage(initial + [s['view'] for s in pool], cfg)
	views = [s['view'] for s in pool]
	full = measure(initial + views)
	loss = [full - measure(initial + views[:i] + views[i + 1:]) for i in range(len(pool))]
	worst = int(np.argmin(loss))
	return pool[:worst] + pool[worst + 1:], pool[worst]
