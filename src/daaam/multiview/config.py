"""Multi-view delayed refinement settings (switches F1-F7 of the design), presets and their validation."""
from dataclasses import dataclass, fields

TRIGGERS = ('none', 'q', 'gamma', 'or', 'joint')
SELECTIONS = ('random', 'q_max', 'coverage_greedy')
FUSIONS = ('none_best', 'naive_llm', 'constrained_llm', 'constrained_llm_viewtext')


@dataclass
class MultiViewConfig:
	"""Off by default; every switch off reproduces upstream DAAAM. `preset` expands to the values below before explicit keys."""
	enabled: bool = False
	preset: str = ""
	seed: int = 0
	# F1 motion blur (A, B, C)
	blur: bool = False
	blur_source: str = "joint"  # odometry | image | joint (F1a)
	blur_frame_filter: bool = False  # F1b: frames with b_rot > blur_max_px are no description candidates
	blur_object_filter: bool = False  # F1c: (frame, object) with b > blur_max_px are not visible to the BLP
	blur_soft_weight: bool = False  # F1d: the BLP maximizes q' instead of q
	blur_fallback: bool = False  # F7: a track ending without an admissible frame is described from its least blurred one
	blur_b0_px: float = 2.5
	blur_max_px: float = 8.0
	blur_joint_a: float = 0.6
	blur_samples: int = 24
	motion_path: str = ""  # <dataset>/.daaam/motion/motion.json (just datasets motion); empty: pose differences
	dataset_fps: float = 0.0  # DAAAM stamps frames index/fps; maps a frame to its dataset row in motion_path
	exposure_s: float = 0.01  # exposure when motion_path has none
	# F2 first description
	decoding: str = "daaam"  # daaam (temperature 0.2) | greedy (temperature 0)
	token_probabilities: bool = False  # F2a: mean token probability p and gamma = exp(alpha (1 - p))
	alpha: float = 1.0
	# F3 trigger (E)
	trigger: str = "none"
	trigger_area: bool = False  # F3a: area ratio < area_min or truncated also triggers
	threshold_mode: str = "quantile"  # quantile (causal, over the values seen so far) | fixed
	threshold_quantile: float = 0.4  # 0.25 (spec): stricter first views, so more objects are refined
	tau_q: float = 0.5
	tau_p: float = 0.6
	tau_s: float = 0.5
	beta: float = 0.5
	area_min: float = 0.002  # mask area / image area
	truncation_max: float = 0.1  # mask pixels on the image border / bbox perimeter; 0.2 (spec), stricter for first views
	# F4 refinement (F, G, H, I)
	refine: bool = False
	refine_timing: str = "delayed"  # immediate | delayed (F4a)
	candidate_source: str = "snapshots"  # snapshots | buffer (F4b)
	selection: str = "coverage_greedy"  # F4c
	refine_budget: int = 2  # F4d
	snapshot_dedup: bool = True  # F4e
	kappa: float = 0.5  # F4f
	n_lost: int = 30
	timeout_frames: int = 0  # 0: no timeout while PENDING
	snapshot_capacity: int = 6
	tau_snap: float = 0.2  # 0.6 (spec) admitted almost nothing: triggered objects have median first-view q' 0.56
	theta_init_deg: float = 5.0  # 20 (spec), 10: 1820 rejections as too close to the first view in a slow hand-held sweep
	snap_truncation_max: float = 0.35  # a snapshot may be cut more than a first view (truncation_max): 10 rejected 1494 frames
	theta_dup_deg: float = 15.0
	crop_margin: float = 0.2
	coverage_directions: int = 128
	sigma_deg: float = 25.0
	phi_reach_deg: float = 40.0
	min_gain: float = 0.02  # 0.05 (spec): views near the first one add little coverage but may still see it better
	tau_cov: float = 0.7
	# F5 fusion (K, L)
	fusion: str = "none_best"
	fusion_model: str = "gpt-5-mini"
	fusion_workers: int = 4  # LLM fusions in flight at once, off the DAM worker
	refine_batch_views: int = 8  # refinement views wait for the next DAM batch, or go alone once this many are waiting
	# F6 consistency u (J): logged only, never used for decisions
	consistency: bool = False
	text_encoder: str = "clip"
	lam: float = 1e-8
	observation_cache_frames: int = 1000
	finish_timeout_s: float = 1800.0  # end of sequence: wait for first descriptions and refinements

	def __post_init__(self):
		self._ready = True

	def __setattr__(self, name, value):
		# a command line override `multiview.preset=x` applies the preset at once, so later overrides still win
		super().__setattr__(name, value)
		if name == 'preset' and value and getattr(self, '_ready', False):
			for key, preset_value in preset(value).items():
				super().__setattr__(key, preset_value)

	@classmethod
	def from_dict(cls, data):
		names = {f.name for f in fields(cls)}
		unknown = set(data) - names
		if unknown:
			raise ValueError('unknown multiview keys: ' + ', '.join(sorted(unknown)))
		return cls(**dict(preset(data.get('preset') or ''), **data))

	def as_dict(self):
		return {f.name: getattr(self, f.name) for f in fields(self)}

	def validate(self):
		if not self.enabled:
			return
		choices = dict(blur_source=('odometry', 'image', 'joint'), decoding=('daaam', 'greedy'), trigger=TRIGGERS,
			threshold_mode=('quantile', 'fixed'), refine_timing=('immediate', 'delayed'), candidate_source=('snapshots', 'buffer'),
			selection=SELECTIONS, fusion=FUSIONS)
		for key, allowed in choices.items():
			if getattr(self, key) not in allowed:
				raise ValueError('multiview.%s must be one of %s' % (key, ', '.join(allowed)))
		if self.trigger in ('gamma', 'or', 'joint') and not self.token_probabilities:
			raise ValueError('multiview.trigger=%s needs multiview.token_probabilities=true (mean token probability)' % self.trigger)
		if self.consistency and not self.token_probabilities:
			raise ValueError('multiview.consistency needs multiview.token_probabilities=true (gamma weights)')
		if min(self.refine_budget, self.snapshot_capacity, self.n_lost, self.fusion_workers, self.refine_batch_views) < 1:
			raise ValueError('multiview.refine_budget, snapshot_capacity, n_lost, fusion_workers and refine_batch_views must be positive')
		if self.motion_path and self.dataset_fps <= 0:
			raise ValueError('multiview.motion_path needs multiview.dataset_fps (frames are stamped index / fps)')


ABLATIONS = {  # one change from ours_full each (section 9)
	'no_blur': dict(blur=False, blur_frame_filter=False, blur_object_filter=False, blur_soft_weight=False, blur_fallback=False),
	'no_blur_filter': dict(blur_frame_filter=False, blur_object_filter=False),
	'blur_odometry': dict(blur_source='odometry'),
	'trigger_q': dict(trigger='q'),
	'trigger_gamma': dict(trigger='gamma'),
	'no_area': dict(trigger_area=False),
	'immediate_buffer': dict(refine_timing='immediate', candidate_source='buffer'),
	'select_q_max': dict(selection='q_max'),
	'select_random': dict(selection='random'),
	'no_dedup': dict(snapshot_dedup=False),
	'fusion_no_viewtext': dict(fusion='constrained_llm'),
	'fusion_naive': dict(fusion='naive_llm'),
	'fusion_none': dict(fusion='none_best'),
	'budget_1': dict(refine_budget=1),
	'budget_3': dict(refine_budget=3),
	'kappa_0.25': dict(kappa=0.25),
	'kappa_1': dict(kappa=1.0),
	'kappa_2': dict(kappa=2.0),
}

PRESETS = {
	'baseline_daaam': dict(enabled=True),
	'qr': dict(enabled=True, trigger='q', refine=True, refine_timing='immediate', candidate_source='buffer', selection='q_max',
		fusion='none_best'),
	'immediate_gamma': dict(enabled=True, token_probabilities=True, trigger='gamma', refine=True, refine_timing='immediate',
		candidate_source='buffer', selection='q_max', fusion='constrained_llm', consistency=True),
	'blur_quality': dict(enabled=True, blur=True, blur_source='joint', blur_frame_filter=True, blur_object_filter=True,
		blur_soft_weight=True, blur_fallback=True),  # motion-blur filtering and q' in the BLP only: no trigger, refinement or fusion
	'ours_full': dict(enabled=True, blur=True, blur_source='joint', blur_frame_filter=True, blur_object_filter=True,
		blur_soft_weight=True, blur_fallback=True, decoding='greedy', token_probabilities=True, trigger='or', trigger_area=True,
		refine=True, refine_timing='delayed', candidate_source='snapshots', selection='coverage_greedy', refine_budget=2,
		snapshot_dedup=True, fusion='constrained_llm_viewtext', consistency=True),
}


def preset(name):
	"""Settings of a preset; `ours_full.<ablation>` is ours_full with one ablation applied."""
	if not name:
		return {}
	base, _, ablation = name.partition('.')
	if base not in PRESETS or (ablation and ablation not in ABLATIONS):
		raise ValueError('unknown multiview preset %r (presets: %s; ablations: ours_full.<%s>)'
			% (name, ', '.join(PRESETS), '|'.join(ABLATIONS)))
	return dict(PRESETS[base], **(ABLATIONS[ablation] if ablation else {}))
