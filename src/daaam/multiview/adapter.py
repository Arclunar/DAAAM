"""Orchestrator side of multi-view delayed refinement. Each described object (semantic ID) moves through
OK | PENDING_UNRESOLVED | PENDING -> REFINING -> REFINED | VIEW_LIMITED (F); PENDING objects collect snapshots (G) until their
track ends, then views are chosen (H, I) and sent to grounding as an MVRefineJob. Every decision is a mv_events.jsonl row and
every object's last state a mv_objects.jsonl row (M)."""
import dataclasses
import json
import math
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from daaam.assignment.workers.min_frames_max_size import MinFramesMaxSizeAssignmentWorker
from daaam.pipeline.models import MVRefineJob

from . import selection as views_selection
from .observe import Motion, crop, observe
from .trigger import REASONS, decide
from .viewtext import condition, sentence


def _plain(value):
	"""JSON-safe copy (numpy scalars and arrays to Python)."""
	if isinstance(value, dict):
		return {k: _plain(v) for k, v in value.items()}
	if isinstance(value, (list, tuple)):
		return [_plain(v) for v in value]
	if isinstance(value, np.ndarray):
		return value.tolist()
	if isinstance(value, np.generic):
		return value.item()
	return value


class MultiViewAdapter:
	def __init__(self, cfg, assignment_config, output_dir, logger, deliver, buffer_frames):
		"""deliver(job) -> bool sends a job to grounding; buffer_frames() -> the orchestrator's recent frames (RGB and tracks)."""
		self.cfg, self.logger, self.deliver, self.buffer_frames = cfg, logger, deliver, buffer_frames
		self._q = SimpleNamespace(config=dict(assignment_config))
		self.motion = Motion(cfg.motion_path, cfg.dataset_fps, cfg.exposure_s) if cfg.blur else None
		self.obs = {}  # (frame_id, track_id) -> observation row
		self.centers = {}  # track_id -> recent world centres
		self.last_seen = {}  # track_id -> frame_id
		self.objects = {}  # semantic_id -> state
		self.awaiting = set()  # semantic IDs prompted whose first description has not come back
		self.prompted = set()  # track IDs sent to DAM
		self.pools = {}  # track_id -> snapshots marked from the track's first frame until its first description decides on refinement
		self.released = set()  # track IDs whose pool was handed over or released: collection does not restart
		self.collect = cfg.refine and cfg.refine_timing == 'delayed' and cfg.candidate_source == 'snapshots'
		self.retained_peak = 0  # most distinct RGB frames held by snapshots at once
		# Before the selection fixes the first view, near views must survive de-duplication: the first view is usually the
		# track's best frame, so a 15 deg cluster kept as one snapshot would be dropped whole as "near the first view".
		self.unplaced = dataclasses.replace(cfg, theta_dup_deg=cfg.theta_init_deg, snapshot_capacity=3 * cfg.snapshot_capacity)
		self.jobs = 0  # refinement jobs in flight
		self.filtered = {}  # track_id -> hard-filtered observations (F7)
		self.fallback = {}  # track_id -> least blurred (row, crop, mask) of a track never sent to DAM (F7)
		self.seen = {'q': [], 'p': [], 'joint': []}
		self.missing_motion = {}
		self.rng = np.random.default_rng(cfg.seed)
		self.frame = 0
		self.last_update = time.monotonic()  # last grounding result, so the end of sequence can stop waiting on a lost batch
		self.lock = threading.RLock()
		self.dir = Path(output_dir)
		self.dir.mkdir(parents=True, exist_ok=True)
		self._events = (self.dir / 'mv_events.jsonl').open('a')
		self._objects = (self.dir / 'mv_objects.jsonl').open('a')

	# ---- logging
	def event(self, kind, **data):
		self._events.write(json.dumps(_plain(dict(kind=kind, frame=self.frame, time=time.time(), **data))) + '\n')
		self._events.flush()

	def _write_object(self, state):
		row = {k: v for k, v in state.items() if k not in ('snapshots', 'since')}
		row['snapshot_count'] = len(state.get('snapshots', []))
		self._objects.write(json.dumps(_plain(row)) + '\n')
		self._objects.flush()

	def _set_state(self, state, new, **data):
		self.event('state', semantic_id=state['semantic_id'], track_id=state['track_id'], old=state.get('state'), new=new, **data)
		state['state'] = new

	# ---- per frame (main thread)
	def observe(self, frame, tracks, masks, object_labels):
		with self.lock:
			self.frame = frame.frame_id
			motion = None
			if self.motion is not None:
				motion, why = self.motion.at(frame)
				if motion is None:
					self.missing_motion[why] = self.missing_motion.get(why, 0) + 1
					if self.missing_motion[why] == 1:
						self.event('motion_unavailable', reason=why)
			h, w = frame.rgb_image.shape[:2]
			for track in tracks:
				mask = masks.get(track.id)
				if mask is None or track.bbox is None or not np.any(mask):
					continue
				mask = mask > 0
				q = MinFramesMaxSizeAssignmentWorker.track_size_reward(self._q, track.get_simplified(), h, w)
				hint = np.median(self.centers[track.id], axis=0) if self.centers.get(track.id) else None
				row = observe(frame, track, mask, q, motion, self.cfg, hint)
				row['semantic_id'] = object_labels.get(track.id)
				self.obs[frame.frame_id, track.id] = row
				if 'center_world' in row:
					self.centers.setdefault(track.id, []).append(row['center_world'])
					del self.centers[track.id][:-50]
				self.last_seen[track.id] = frame.frame_id
				state = self.objects.get(row['semantic_id'])
				if state is not None and state['state'] == 'PENDING' and state['track_id'] == track.id and self.cfg.candidate_source == 'snapshots':
					self._admit(state, row, frame.rgb_image, mask)
				elif self.collect and track.id not in self.released:   # every track from its first frame: views a refinement may need
					pool = self.pools.setdefault(track.id, dict(track_id=track.id, snapshots=[]))
					pool['semantic_id'] = row['semantic_id']
					self._admit(pool, row, frame.rgb_image, mask)
				if self.cfg.blur_fallback and track.id not in self.prompted:
					old = self.fallback.get(track.id)
					key = lambda r: (math.inf if r['b'] is None else r['b'], -r['q'])
					if old is None or key(row) < key(old[0]):
						self.fallback[track.id] = (row, frame.rgb_image) + crop(mask, self.cfg.crop_margin) + (mask.shape,)
			oldest = frame.frame_id - self.cfg.observation_cache_frames
			if oldest > 0 and frame.frame_id % 100 == 0:
				self.obs = {key: row for key, row in self.obs.items() if key[0] > oldest}

	def weigh(self, frame_id, simplified):
		"""Blur filtering of one frame's tracks before they reach the BLP (C); tracking and mapping are unaffected."""
		if not self.cfg.blur:
			return simplified
		with self.lock:
			kept, rows = [], [self.obs.get((frame_id, t.id)) for t in simplified]
			frame_bad = self.cfg.blur_frame_filter and any(r is not None and r['b_rot'] is not None and r['b_rot'] > self.cfg.blur_max_px for r in rows)
			if frame_bad:
				self.event('blur_filtered', level='frame', frame_id=frame_id, b_rot=max(r['b_rot'] for r in rows if r and r['b_rot'] is not None))
			for track, row in zip(simplified, rows):
				object_bad = self.cfg.blur_object_filter and row is not None and row['b'] is not None and row['b'] > self.cfg.blur_max_px
				if frame_bad or object_bad:
					self.filtered[track.id] = self.filtered.get(track.id, 0) + 1
					if object_bad and not frame_bad:
						self.event('blur_filtered', level='object', frame_id=frame_id, track_id=track.id, b=row['b'])
					continue
				if self.cfg.blur_soft_weight and row is not None:
					track.quality_weight = row['w_blur']
				kept.append(track)
			return kept

	def tick(self, object_labels):
		"""Track ends of PENDING objects (and F7 fallbacks); called once per frame."""
		with self.lock:
			for state in [s for s in self.objects.values() if s['state'] == 'PENDING']:
				lost = self.frame - self.last_seen.get(state['track_id'], self.frame)
				if lost > self.cfg.n_lost:
					self._end(state, 'track_lost')
				elif self.cfg.timeout_frames and self.frame - state['since'] > self.cfg.timeout_frames:
					self._end(state, 'timeout')
			for track_id in [t for t in self.fallback if self.frame - self.last_seen.get(t, self.frame) > self.cfg.n_lost]:
				self._fallback(track_id, object_labels)
			if self.pools:
				window = len(self.buffer_frames())   # a track older than the query window can no longer be sent to DAM
				for track_id in [t for t in self.pools if t not in self.prompted and self.frame - self.last_seen.get(t, self.frame) > window]:
					del self.pools[track_id]
					self.released.add(track_id)
			if self.frame % 50 == 0:
				held = [s for pool in self.pools.values() for s in pool['snapshots']] + [s for st in self.objects.values()
					if st['state'] == 'PENDING' for s in st.get('snapshots', [])]
				self.retained_peak = max(self.retained_peak, len({id(s['image']) for s in held}))

	# ---- grounding results (correction thread)
	def on_prompt(self, track_ids, object_labels, frame_id=None):
		with self.lock:
			for track_id in track_ids:
				self.prompted.add(track_id)
				self.fallback.pop(track_id, None)
				if object_labels.get(track_id) is not None:
					self.awaiting.add(object_labels[track_id])
				pool, initial = self.pools.get(track_id), self.obs.get((frame_id, track_id))
				if pool is not None and initial is not None:   # the selection fixed the first view: no need to wait for its description
					pool['initial'] = initial
					self._drop_near_initial(pool, initial)


	def on_record(self, data):
		with self.lock:
			self.last_update = time.monotonic()
			if data['kind'] == 'initial':
				self._initial(data)
			else:
				self._final(data)

	def _initial(self, data):
		label, frame_id, p = data['semantic_id'], data['frame_id'], data.get('probability')
		self.awaiting.discard(label)
		row = next((r for (f, _), r in self.obs.items() if f == frame_id and r['semantic_id'] == label), None)
		if row is None:
			self.event('initial_without_observation', semantic_id=label, frame_id=frame_id)
			return
		# The first description returns a median 190 frames after its view, when most tracks have ended, so the pool has been
		# marking views since the track began; a second first description (re-prompt) keeps what the PENDING object holds.
		previous = self.objects.get(label)
		pool = self.pools.pop(row['track_id'], None)
		self.released.add(row['track_id'])
		if pool is None and previous is not None and previous['state'] == 'PENDING' and previous['track_id'] == row['track_id']:
			pool = previous
		gamma = None if p is None or not self.cfg.token_probabilities else math.exp(self.cfg.alpha * (1 - p))
		p_used = p if self.cfg.token_probabilities else None
		reasons = decide(row['q_prime'], p_used, row['area_ratio'], row['truncated'], self.cfg, self.seen)
		initial = dict(row, text=data['text'], p=p_used, gamma=gamma, stage='initial')
		state = dict(semantic_id=label, track_id=row['track_id'], state='TRACKING', reasons=reasons,
			reasons_zh=[REASONS[r][0] for r in reasons], initial=initial, views=[], snapshots=[], since=self.frame,
			description=data['text'])
		self.objects[label] = state
		self.event('first_description', semantic_id=label, track_id=row['track_id'], frame_id=frame_id, p=p_used, gamma=gamma,
			q=row['q'], q_prime=row['q_prime'], area_ratio=row['area_ratio'], truncated=row['truncated'], reasons=reasons)
		if not reasons:
			self._set_state(state, 'OK')
			self._write_object(state)
		elif not self.cfg.refine:
			self._set_state(state, 'PENDING_UNRESOLVED')
			self._write_object(state)
		elif self.cfg.refine_timing == 'immediate':
			self._end(state, 'immediate')
		else:
			self._set_state(state, 'PENDING')
			if pool is not None:
				self._take_pool(state, pool)
			elif self.cfg.candidate_source == 'snapshots':  # no prompt-time pool: frames tracked while the description was being made
				for frame in self.buffer_frames():
					if frame.frame_id > frame_id:
						track = next((t for t in frame.tracks if t.id == state['track_id']), None)
						row_later = self.obs.get((frame.frame_id, state['track_id']))
						if track is not None and row_later is not None:
							self._admit(state, row_later, frame.rgb_image, track.get_mask() > 0)

	def _take_pool(self, state, pool):
		"""A PENDING object takes over its track's snapshots, checked against the view its description came from (a re-prompt
		may have chosen another one than the last selection)."""
		self._drop_near_initial(pool, state['initial'])
		state['snapshots'], state['rejected'] = pool['snapshots'], pool.get('rejected', {})

	def _drop_near_initial(self, pool, initial_row):
		"""Snapshots of the first view's frame, or within theta_init of it, add no new view."""
		initial = self._direction(initial_row, pool['track_id'])
		rejected, kept = pool.setdefault('rejected', {}), []
		for s in pool['snapshots']:
			d = s['view']['direction']
			if s['view']['frame_id'] == initial_row['frame_id'] or (d is not None and initial is not None
					and views_selection.angle(d, initial) < math.radians(self.cfg.theta_init_deg)):
				rejected['near_initial_view'] = rejected.get('near_initial_view', 0) + 1
			else:
				kept.append(s)
		pool['snapshots'] = []   # the first view is known now: the usual de-duplication and capacity
		for s in sorted(kept, key=lambda s: -s['view']['q_prime']):
			if self.cfg.snapshot_dedup and s['view']['direction'] is not None:
				pool['snapshots'], dropped = views_selection.dedup_add(pool['snapshots'], s, self.cfg)
				for old in dropped:
					self.event('snapshot_dropped', semantic_id=pool['semantic_id'], frame_id=old['view']['frame_id'], reason='duplicate_view')
			else:
				pool['snapshots'].append(s)
		self._fit(pool, initial, self.cfg)

	def _final(self, data):
		self.jobs -= 1
		state = self.objects.get(data['semantic_id'])
		if state is None:
			return
		if data.get('error'):  # the worker failed: the first description stays
			self._set_state(state, 'VIEW_LIMITED', reason='refinement_error', error=data['error'])
			self._write_object(state)
			return
		for view, result in zip(state['views'], data.get('views', [])):
			view.update(text=result['text'], p=result['p'], gamma=result['gamma'])
		fused = data['fusion']
		state.update(final=fused, description=fused['description'], u_before=data.get('u_before'), u_after=data.get('u_after'))
		self.event('fusion', semantic_id=state['semantic_id'], **{k: v for k, v in fused.items() if k != 'facts_used'})
		if data.get('u_after') is not None:
			self.event('consistency', semantic_id=state['semantic_id'], u_before=data.get('u_before'), u_after=data['u_after'])
		self._set_state(state, 'VIEW_LIMITED' if state.get('kind') == 'fallback' else 'REFINED')
		self._write_object(state)

	# ---- snapshots and selection
	def _direction(self, row, track_id):
		"""Object centre -> camera unit vector, centre = median of the track's recent world centres."""
		if 'camera_world' not in row or not self.centers.get(track_id):
			return None
		d = np.asarray(row['camera_world']) - np.median(self.centers[track_id], axis=0)
		n = np.linalg.norm(d)
		return d / n if n > 1e-6 else None

	def _admit(self, state, row, image, mask):
		"""Snapshot admission (G): q' >= tau_snap, large enough and uncut, b <= b_max, >= theta_init from the initial view."""
		why = []
		if row['q_prime'] < self.cfg.tau_snap:
			why.append('low_q_prime')
		if row['area_ratio'] < self.cfg.area_min or row['edge_ratio'] > self.cfg.snap_truncation_max:
			why.append('small_or_truncated')
		if self.cfg.blur and row['b'] is not None and row['b'] > self.cfg.blur_max_px:
			why.append('blurred')
		direction = self._direction(row, state['track_id'])
		initial = self._direction(state['initial'], state['track_id']) if state.get('initial') else None   # unknown until the track is selected
		if direction is not None and initial is not None and views_selection.angle(direction, initial) < math.radians(self.cfg.theta_init_deg):
			why.append('near_initial_view')
		if why:
			rejected = state.setdefault('rejected', {})
			for reason in why:
				rejected[reason] = rejected.get(reason, 0) + 1
			return
		item = dict(view=dict(row, direction=direction))
		cfg = self.cfg if state.get('initial') else self.unplaced
		pool, dropped = (views_selection.dedup_add(state['snapshots'], item, cfg) if cfg.snapshot_dedup and direction is not None
			else (state['snapshots'] + [item], []))
		for old in dropped:
			self.event('snapshot_dropped', semantic_id=state['semantic_id'], frame_id=old['view']['frame_id'], reason='duplicate_view')
		if any(s is item for s in pool):  # copy pixels only for a snapshot that survived de-duplication
			item['image'] = image   # the whole frame, so DAM sees the context as for a first description
			item['mask'], item['offset'] = crop(mask, self.cfg.crop_margin)
			item['shape'] = mask.shape  # with the offset, the window mask goes back into a full-frame mask
			self.event('snapshot_admitted', semantic_id=state['semantic_id'], frame_id=row['frame_id'], q_prime=row['q_prime'])
		state['snapshots'] = pool
		self._fit(state, initial, cfg)

	def _fit(self, state, initial, cfg):
		"""Over capacity: evict the snapshot whose removal loses the least coverage (the lowest q' without geometry)."""
		pool = state['snapshots']
		while len(pool) > cfg.snapshot_capacity:
			views = [dict(state['initial'], direction=initial)] if initial is not None else []
			if views and all(s['view']['direction'] is not None for s in pool):
				pool, old = views_selection.evict(pool, views, cfg)
			else:
				old = min(pool, key=lambda s: s['view']['q_prime'])
				pool = [s for s in pool if s is not old]
			self.event('snapshot_evicted', semantic_id=state['semantic_id'], frame_id=old['view']['frame_id'])
		state['snapshots'] = pool

	def _buffer_candidates(self, state):
		items = []
		for frame in self.buffer_frames():
			if frame.frame_id == state['initial']['frame_id']:
				continue
			track = next((t for t in frame.tracks if t.id == state['track_id']), None)
			row = self.obs.get((frame.frame_id, state['track_id']))
			if track is not None and row is not None:
				full = track.get_mask() > 0
				mask_crop, offset = crop(full, self.cfg.crop_margin)
				items.append(dict(view=dict(row, direction=None), image=frame.rgb_image, mask=mask_crop, offset=offset, shape=full.shape))
		return items

	def _end(self, state, reason):
		"""Track end (or immediate trigger): choose views, then send a refinement job or mark the object VIEW_LIMITED."""
		self._set_state(state, 'REFINING', reason=reason)
		items = state['snapshots'] if self.cfg.candidate_source == 'snapshots' else self._buffer_candidates(state)
		state['snapshots'] = []
		if self.cfg.blur:
			items = [s for s in items if s['view']['b'] is None or s['view']['b'] <= self.cfg.blur_max_px]
		initial = dict(state['initial'], direction=self._direction(state['initial'], state['track_id']))
		for item in items:  # directions against the final centre estimate
			item['view']['direction'] = self._direction(item['view'], state['track_id'])
		strategy = self.cfg.selection
		usable = [s for s in items if s['view']['direction'] is not None]
		if strategy == 'coverage_greedy' and (initial['direction'] is None or not usable):
			strategy = 'q_max'  # no geometry (pose/depth): coverage is undefined
		pool = usable if strategy == 'coverage_greedy' else items
		chosen, gains, before, after = views_selection.SELECTIONS[strategy](
			[initial], [s['view'] for s in pool], self.cfg, self.rng)
		picked = [next(s for s in pool if s['view'] is v) for v in chosen]
		state['selection'] = dict(strategy=strategy, source=self.cfg.candidate_source, candidates=len(items), coverage_before=before,
			coverage_after=after, gains=gains, reason=reason)
		self.event('selection', semantic_id=state['semantic_id'], frame_ids=[s['view']['frame_id'] for s in picked],
			**{k: v for k, v in state['selection'].items()})
		if not picked:
			self._set_state(state, 'VIEW_LIMITED', reason='no_admissible_view')
			self._write_object(state)
			return
		initial_condition = condition(initial)
		state['initial'].update(condition=initial_condition,
			condition_text=sentence(initial_condition, [REASONS[r][1] for r in state['reasons']]))
		views = []
		for s in picked:
			c = condition(s['view'], initial)
			path = self._save_crop(state['semantic_id'], s)
			views.append(dict({k: v for k, v in s['view'].items() if k != 'direction'}, stage='refine', condition=c,
				condition_text=sentence(c), snapshot_path=path, angle_to_initial_deg=c.get('angle_to_initial_deg')))
		state['views'] = views
		job = MVRefineJob(state['semantic_id'], state['track_id'], kind='refine',
			views=[dict(image=s['image'], mask=s['mask'], offset=s['offset'], shape=s['shape'], frame_id=v['frame_id'], timestamp=v['timestamp'],
				q_prime=v['q_prime'], condition_text=v['condition_text']) for s, v in zip(picked, views)],
			initial={k: state['initial'].get(k) for k in ('frame_id', 'timestamp', 'text', 'p', 'gamma', 'q_prime', 'condition_text')})
		self._send(state, job)

	def _fallback(self, track_id, object_labels):
		"""F7: a track that ended without being sent to DAM after blur filtering is described from its least blurred view."""
		row, image, mask, offset, shape = self.fallback.pop(track_id)
		label = object_labels.get(track_id)
		if not self.filtered.get(track_id) or label is None or label in self.objects:
			return
		state = dict(semantic_id=label, track_id=track_id, state='TRACKING', kind='fallback', reasons=['blur_only'],
			reasons_zh=['仅有模糊观测'], initial=None, snapshots=[], since=self.frame)
		self.objects[label] = state
		c = condition(row)
		view = dict(row, stage='fallback', condition=c, condition_text=sentence(c), snapshot_path=None)
		view['snapshot_path'] = self._save_crop(label, dict(view=view, image=image, mask=mask, offset=offset))
		state['views'] = [view]
		self._set_state(state, 'REFINING', reason='blur_fallback')
		self._send(state, MVRefineJob(label, track_id, kind='fallback', initial=None, views=[dict(image=image, mask=mask, offset=offset, shape=shape,
			frame_id=row['frame_id'], timestamp=row['timestamp'], q_prime=row['q_prime'], condition_text=view['condition_text'])]))

	def _send(self, state, job):
		if self.deliver(job):
			self.jobs += 1
		else:
			self._set_state(state, 'VIEW_LIMITED', reason='delivery_failed')
			self._write_object(state)

	def _save_crop(self, label, item):
		relative = 'mv_snapshots/%s_%s.jpg' % (label, item['view']['frame_id'])
		path = self.dir / relative
		path.parent.mkdir(parents=True, exist_ok=True)
		(x0, y0), (h, w) = item['offset'], item['mask'].shape
		overlay = item['image'][y0:y0 + h, x0:x0 + w].copy()   # the preview stays a crop
		edge = cv2.morphologyEx(item['mask'].astype(np.uint8), cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)) > 0
		overlay[edge] = (255, 255, 255)
		return relative if cv2.imwrite(str(path), cv2.cvtColor(overlay, cv2.COLOR_RGB2BGR)) else None

	# ---- end of sequence
	def finish(self, object_labels):
		"""Sequence end counts as every track's end."""
		with self.lock:
			for state in [s for s in self.objects.values() if s['state'] == 'PENDING']:
				self._end(state, 'sequence_end')
			self.pools.clear()  # descriptions that never came back
			for track_id in list(self.fallback):
				self._fallback(track_id, object_labels)

	def summary(self):
		with self.lock:
			counts = {}
			for state in self.objects.values():
				counts[state['state']] = counts.get(state['state'], 0) + 1
			return dict(states=counts, awaiting=len(self.awaiting), jobs_in_flight=self.jobs, missing_motion=self.missing_motion,
				blur_filtered_tracks=len(self.filtered), retained_frames_peak=self.retained_peak)

	def close(self):
		with self.lock:
			self.event('summary', **self.summary())
			(self.dir / 'mv_summary.json').write_text(json.dumps(_plain(dict(self.summary(), config=self.cfg.as_dict())), indent=2) + '\n')
			self._events.close()
			self._objects.close()
