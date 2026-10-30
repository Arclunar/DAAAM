import numpy as np
from typing import Dict, List, Tuple, Union, Set, Optional

from daaam.assignment.workers.min_frames import MinFramesAssignmentWorker
from daaam.utils.performance import performance_measure
from daaam.utils.logging import PipelineLogger
from daaam.grounding.models import ObjectAnnotation
from daaam.tracking.models import SimplifiedTrack
from daaam.assignment.models import AssignmentTask, SelectedGroup
import cvxpy as cp

class MinFramesMaxSizeAssignmentWorker(MinFramesAssignmentWorker):
	"""DAM-specific assignment worker that minimizes frames and covers all unlabeled tracks."""
	
	def __init__(self, assignment_task_queue, selected_groups_queue, stop_event, config, name=None):
		super().__init__(assignment_task_queue, selected_groups_queue, stop_event, config, name)
	
	def process_assignment_task(self, assignment_task: AssignmentTask) -> Optional[List[SelectedGroup]]:
		"""Process assignment task and return selected groups."""
		frame_history = assignment_task.track_history
		corrections = assignment_task.corrections
		frame_dims = assignment_task.frame_dims
		object_labels = assignment_task.object_labels
		prompted_track_ids = set(assignment_task.prompted_track_ids)
		start_frame_count = assignment_task.start_frame_count
		
		self.worker_logger.info(f"Data unpacked: History length={len(frame_history)}, StartFrame={start_frame_count}")
		
		if not frame_history:
			self.worker_logger.warning("Received empty frame history.")
			return None
		
		return self.select_prompt_images(
			frame_history,
			corrections,
			frame_dims,
			object_labels,
			prompted_track_ids,
			start_frame_count
		)
	
	def select_prompt_images(
		self,
		frame_history: List[List[SimplifiedTrack]],
		corrections: Dict[int, ObjectAnnotation],
		frame_dims: Tuple[int, int],
		object_labels: Dict[int, int],
		prompted_track_ids: Set[int],
		start_frame_count: int
	) -> Optional[List[SelectedGroup]]:
		"""DAM-specific selection that minimizes frames and covers all unlabeled tracks without overlap constraints."""

		if self.config.get("uq", {}).get("enabled", False):
			return self.select_prompt_images_uq(
				frame_history, corrections, frame_dims, object_labels, prompted_track_ids, start_frame_count)

		frame_height, frame_width = frame_dims

		with performance_measure(
			"[AssignmentWorker]: setup optimization problem", self.worker_logger.debug
		):
			
			greedy_selected_groups, valid_tracks, qualified_track_ids, track_observations, tracks_by_frame = self.select_prompt_images_greedy(
			frame_history,
			corrections,
			frame_dims,
			object_labels,
			prompted_track_ids,
			start_frame_count
			)
			min_number_frames = len(greedy_selected_groups)
			if not qualified_track_ids:
				self.worker_logger.warning("WARNING: No qualified tracks found. Skipping optimization.")
				return None

			# setup optimization variables
			self.worker_logger.debug("Setting up optimization variables...")
			n_frames = len(frame_history)
			n_tracks = len(qualified_track_ids)
			qualified_track_ids_list = list(
				qualified_track_ids
			)  # consistent ordering
			track_id_to_idx = {
				tid: idx for idx, tid in enumerate(qualified_track_ids_list)
			}

			frame_vars = cp.Variable(n_frames, boolean=True)
			track_frame_vars = cp.Variable((n_tracks, n_frames), boolean=True)

			track_sizes = np.zeros((n_tracks, n_frames))
			for local_frame_idx, tracks in tracks_by_frame.items():
				for track in tracks:
					if track.id in qualified_track_ids:
						track_sizes[
							track_id_to_idx[track.id], local_frame_idx
						] = self.track_size_reward(track, frame_height, frame_width) * track.quality_weight

			constraints = []
			constraints.append(cp.sum(track_frame_vars, axis=1) == 1)
			for track_id in qualified_track_ids:
				track_idx = track_id_to_idx[track_id]
				valid_frames = track_observations[track_id]
				for local_frame_idx in range(n_frames):
					if local_frame_idx not in valid_frames:
						constraints.append(
							track_frame_vars[track_idx, local_frame_idx] == 0
						)
			for local_frame_idx in range(n_frames):
				constraints.append(
					track_frame_vars[:, local_frame_idx] <= frame_vars[local_frame_idx]
				)

		#### Hierarchical Objective ####
		selected_groups_output = []
		try:
			with performance_measure(
				"[AssignmentWorker]: optimization problem", self.worker_logger.debug
			):
				min_frames = min_number_frames + self.config.get("min_frame_margin_slack", 1)
				self.worker_logger.debug(f"Minimum frames from greedy optimization: {min_frames}")
				constraints.append(cp.sum(frame_vars) == min_frames)
				obj2 = cp.sum(cp.multiply(track_frame_vars, track_sizes))
				prob2 = cp.Problem(cp.Maximize(obj2), constraints)
				self.worker_logger.debug("Solving optimization problem (Maximize Size)...")
				try:
					prob2.solve(cp.GLPK_MI, glpk={"tm_lim": 10000})  # 10s timeout in ms
				except Exception:
					self.worker_logger.warning("GLPK_MI failed, falling back to SCIPY")
					prob2.solve(cp.SCIPY, scipy_options={"time_limit": 10.0})
				self.worker_logger.info(f"Optimization problem 2 status: {prob2.status}, Value: {prob2.value}")

			if prob2.status not in [cp.OPTIMAL, cp.OPTIMAL_INACCURATE]:
				self.worker_logger.warning(f"MIP solver failed (status: {prob2.status}), using greedy solution")
				return greedy_selected_groups

			#### Extract results ####
			self.worker_logger.debug("Extracting optimization results...")
			if (
				track_frame_vars.value is not None
				and frame_vars.value is not None
			):
				frame_assignments = track_frame_vars.value > 0.5
				selected_frame_indices = np.where(frame_vars.value > 0.5)[0]
				self.worker_logger.debug(f"Selected frame indices: {selected_frame_indices}")

				for best_local_frame_idx in selected_frame_indices:
					assigned_track_indices = np.where(
						frame_assignments[:, best_local_frame_idx]
					)[0]
					assigned_track_ids = [
						qualified_track_ids_list[idx]
						for idx in assigned_track_indices
					]
					if assigned_track_ids:
						global_frame_id = start_frame_count + int(best_local_frame_idx)
						selected_groups_output.append(
							SelectedGroup(
								global_frame_id=global_frame_id,
								track_ids=assigned_track_ids,
							)
						)
						self.worker_logger.debug(
							f"Added group: start_frame={start_frame_count}, "
							f"rel_local_frame_idx={best_local_frame_idx}, tracks={assigned_track_ids}"
						)
			else:
				self.worker_logger.warning("Optimization variables have no value, using greedy solution")
				return greedy_selected_groups

		except Exception as e:
			self.worker_logger.error(f"ERROR during optimization/extraction: {e}")
			import traceback
			self.worker_logger.error(traceback.format_exc())
			self.worker_logger.warning("Using greedy solution as fallback")
			return greedy_selected_groups

		return selected_groups_output



	def select_prompt_images_greedy(
		self,
		frame_history: List[List[SimplifiedTrack]],
		corrections: Dict[int, ObjectAnnotation],
		frame_dims: Tuple[int, int],
		object_labels: Dict[int, int],
		prompted_track_ids: Set[int],
		start_frame_count: int
	) -> Optional[List[SelectedGroup]]:
		"""DAM-specific selection that minimizes frames and covers all unlabeled tracks without overlap constraints."""
		
		self.worker_logger.debug("Starting DAM optimization setup...")
		
		if not frame_dims:
			self.worker_logger.error("Frame dimensions missing!")
			return None

		# Build track observations and filter qualified tracks
		tracks_by_frame = {}
		track_observations = {}
		valid_tracks = []
		for frame_idx, record in enumerate(frame_history):
			valid_tracks_frame = []
			for track in record:
				if isinstance(track, SimplifiedTrack):
					valid_tracks_frame.append(track)
					track_id = track.id
					if track_id not in track_observations:
						track_observations[track_id] = set()
					track_observations[track_id].add(frame_idx)
			if valid_tracks_frame:
				tracks_by_frame[frame_idx] = valid_tracks_frame
				valid_tracks.extend(valid_tracks_frame)

		# filter qualified tracks, only tracks that haven't been previously labeled
		def is_track_qualified(track):
			tid = track.id
			semantic_id = object_labels.get(tid, -1)

			# if correction for this semantic_id, check if "unknown"
			if semantic_id in corrections:
				correction = corrections[semantic_id]
				# allow re-prompting if correction label "unknown"
				is_unknown = correction.semantic_label.lower() == "unknown"
				
				if not is_unknown:
					return False  # skip tracks with non-unknown corrections
				
			if not track.depth_valid:
				return False
			# other conditions
			return (len(track_observations[tid]) >= self.min_obs_per_track 
					and tid not in prompted_track_ids)

		qualified_track_ids = {
			track.id for track in valid_tracks if is_track_qualified(track)
		}

		self.worker_logger.info(f"Qualified tracks count: {len(qualified_track_ids)}")

		if not qualified_track_ids:
			self.worker_logger.warning("No qualified tracks found. Skipping optimization.")
			return None

		# DAM assignment logic: minimize frames while covering all tracks
		self.worker_logger.debug("Using DAM assignment logic (greedy frame selection)...")
		
		uncovered_track_ids = qualified_track_ids.copy()
		selected_groups = []
		
		while uncovered_track_ids:
			# frame that covers the most uncovered tracks
			best_local_frame_idx = None
			best_tracks_in_frame = []
			best_coverage = 0
			
			for frame_idx, frame_tracks in tracks_by_frame.items():
				tracks_in_frame = [t.id for t in frame_tracks if t.id in uncovered_track_ids]
				
				if len(tracks_in_frame) > best_coverage:
					best_coverage = len(tracks_in_frame)
					best_local_frame_idx = frame_idx
					best_tracks_in_frame = tracks_in_frame
			
			if best_local_frame_idx is None or not best_tracks_in_frame:
				self.worker_logger.debug("No more frames can cover remaining tracks.")
				break
			
			# split tracks into batches of N_masks_per_batch (mostly len(best_tracks_in_frame) <= N_masks_per_batch anyway)
			for i in range(0, len(best_tracks_in_frame), self.N_masks_per_batch):
				batch_tracks = best_tracks_in_frame[i:i + self.N_masks_per_batch]
				global_frame_id = start_frame_count + int(best_local_frame_idx)
				selected_groups.append(SelectedGroup(
					global_frame_id=global_frame_id,
					track_ids=batch_tracks,
				))
				self.worker_logger.debug(f"Added batch: frame_idx={best_local_frame_idx}, tracks={len(batch_tracks)}")
			
			# remove covered tracks
			uncovered_track_ids.difference_update(best_tracks_in_frame)
			self.worker_logger.debug(f"Covered {len(best_tracks_in_frame)} tracks, {len(uncovered_track_ids)} remaining")

		self.worker_logger.info(f"DAM assignment complete. Selected {len(selected_groups)} groups.")
		return selected_groups, valid_tracks, qualified_track_ids, track_observations, tracks_by_frame


	def select_prompt_images_uq(
		self,
		frame_history: List[List[SimplifiedTrack]],
		corrections: Dict[int, ObjectAnnotation],
		frame_dims: Tuple[int, int],
		object_labels: Dict[int, int],
		prompted_track_ids: Set[int],
		start_frame_count: int
	) -> Optional[List[SelectedGroup]]:
		"""UQ-DAAAM r-assignment: max sum q_ij y_ij s.t. sum_i y_ij = r_j, y_ij <= v_ij, y_ij <= x_i.

		r_j = min(r, visible frames), or 1 when fewer than min_views frames see the track (single-view upstream path).
		Frame budget = greedy r-cover frame count + min_frame_margin_slack, capped at the window length.
		For each track the top-B visible frames it was not assigned are returned as "candidate" groups for refinement.
		"""
		uq = self.config["uq"]
		greedy = self.select_prompt_images_greedy(
			frame_history, corrections, frame_dims, object_labels, prompted_track_ids, start_frame_count)
		if not greedy:
			return None
		_, _, qualified_track_ids, track_observations, tracks_by_frame = greedy
		frame_height, frame_width = frame_dims
		track_ids = sorted(qualified_track_ids)
		quality = {
			(track.id, frame_idx): float(self.track_size_reward(track, frame_height, frame_width))
			for frame_idx, tracks in tracks_by_frame.items() for track in tracks if track.id in qualified_track_ids
		}
		demand = {}
		for tid in track_ids:
			visible = len(track_observations[tid])
			demand[tid] = min(uq["views_per_object"], visible) if visible >= uq["min_views"] else 1

		# greedy r-cover: frame covering most tracks that still need a view not taken from this frame
		remaining, used, greedy_frames = dict(demand), {tid: set() for tid in track_ids}, 0
		while any(remaining.values()):
			coverage = {
				frame_idx: [t.id for t in tracks if remaining.get(t.id, 0) > 0 and frame_idx not in used[t.id]]
				for frame_idx, tracks in tracks_by_frame.items()
			}
			best = max(coverage, key=lambda frame_idx: (len(coverage[frame_idx]), -frame_idx))
			if not coverage[best]:
				break
			greedy_frames += 1
			for tid in coverage[best]:
				remaining[tid] -= 1
				used[tid].add(best)
		assigned = used  # greedy assignment is the fallback

		n_frames, n_tracks = len(frame_history), len(track_ids)
		frame_vars = cp.Variable(n_frames, boolean=True)
		track_frame_vars = cp.Variable((n_tracks, n_frames), boolean=True)
		rewards, visible = np.zeros((n_tracks, n_frames)), np.zeros((n_tracks, n_frames))
		for (tid, frame_idx), q in quality.items():
			rewards[track_ids.index(tid), frame_idx] = q
			visible[track_ids.index(tid), frame_idx] = 1
		budget = min(n_frames, greedy_frames + self.config.get("min_frame_margin_slack", 1))
		constraints = [
			cp.sum(track_frame_vars, axis=1) == np.array([demand[tid] for tid in track_ids]),
			track_frame_vars <= visible,
			cp.sum(frame_vars) == budget,
		]
		constraints += [track_frame_vars[:, i] <= frame_vars[i] for i in range(n_frames)]
		problem = cp.Problem(cp.Maximize(cp.sum(cp.multiply(track_frame_vars, rewards))), constraints)
		limit = float(uq.get("solver_time_limit_s", 15.0))
		try:
			try:
				problem.solve(cp.GLPK_MI, glpk={"tm_lim": int(limit * 1000)})
			except Exception:
				self.worker_logger.warning("GLPK_MI failed, falling back to SCIPY")
				problem.solve(cp.SCIPY, scipy_options={"time_limit": limit})
			self.worker_logger.info(f"UQ r-assignment status: {problem.status}, value: {problem.value}, frame budget: {budget}")
			if problem.status in [cp.OPTIMAL, cp.OPTIMAL_INACCURATE] and track_frame_vars.value is not None:
				selection = track_frame_vars.value > 0.5
				assigned = {tid: set(np.where(selection[k])[0].tolist()) for k, tid in enumerate(track_ids)}
			else:
				self.worker_logger.warning("UQ r-assignment MIP failed, using greedy r-cover")
		except Exception as e:
			self.worker_logger.error(f"UQ r-assignment failed ({e}), using greedy r-cover")

		groups: Dict[Tuple[str, int], Dict[int, float]] = {}
		for tid in track_ids:
			for frame_idx in assigned[tid]:
				groups.setdefault(("view", frame_idx), {})[tid] = quality[tid, frame_idx]
			if demand[tid] < uq["min_views"]:
				continue
			unused = sorted((f for f in track_observations[tid] if f not in assigned[tid]),
				key=lambda f: (-quality[tid, f], f))[:uq["refine_budget"]]
			for frame_idx in unused:
				groups.setdefault(("candidate", frame_idx), {})[tid] = quality[tid, frame_idx]
		return [
			SelectedGroup(start_frame_count + int(frame_idx), list(scores), role=role, quality=scores)
			for (role, frame_idx), scores in sorted(groups.items(), key=lambda item: (item[0][0] != "view", item[0][1]))
		]

	def track_size_reward(self, track: SimplifiedTrack, frame_height: int, frame_width: int) -> float:
		"""
		Compute reward for track selection optimized for DAM grounding.
		
		Rewards tracks that are:
		1. Centered in frame (better for VLM grounding)
		2. Appropriately sized (not too small, not too large)
		"""
		min_mask_region_area = self.config.get("min_mask_region_area", 300)
		position_score_weight = self.config.get("position_score_weight", 0.5)
		size_score_weight = self.config.get("size_score_weight", 0.5)

		x1, y1, x2, y2 = track.bbox
		cx = 0.5 * (x1 + x2)
		cy = 0.5 * (y1 + y2)
	
		# Normalized coordinates [0, 1]
		px = cx / frame_width
		py = cy / frame_height

		# Compute position entropy (max at center p=0.5, min at edges p={0,1})
		position_entropy =  -px * np.log(px + 1e-8) - (1-px) * np.log(1-px + 1e-8)
		position_entropy += -py * np.log(py + 1e-8) - (1-py) * np.log(1-py + 1e-8)
		
		# favors centered objects
		position_score = position_entropy / np.log(4)  # normalize to [0,1]

		# Size score: tanh saturates gracefully for large objects
		size_score = np.tanh((track.region_area) / (min_mask_region_area * 10.0))

		# self.worker_logger.debug(f"Track {track.id} - Position score: {position_score:.3f}/{position_score_weight}, Size score: {size_score:.3f}/{size_score_weight}")

		return position_score_weight * position_score + size_score_weight * size_score