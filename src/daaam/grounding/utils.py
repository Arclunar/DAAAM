import numpy as np
import multiprocessing as mp
import queue
from typing import List, Dict, Set, Optional, Union
from PIL import Image as PILImage

from daaam.tracking.models import Track
from daaam.grounding.models import ImageAnnotation, ObjectAnnotation


def prepare_masks_from_tracks(
	prompt_tracks: List[Track],
	object_labels: Dict[int, int],
	worker_id: str,
) -> tuple[List[PILImage.Image], List[int]]:
	"""Prepare PIL masks and semantic IDs from track data."""
	masks = []
	semantic_ids = []
	
	for track in prompt_tracks:
		semantic_id = object_labels.get(track.id)
		if semantic_id is not None:
			if isinstance(track.segmentation, np.ndarray):
				# Convert to PIL mask (binary)
				mask_pil = PILImage.fromarray((track.segmentation * 255).astype(np.uint8), mode='L')
				masks.append(mask_pil)
				semantic_ids.append(semantic_id)
			else:
				print(
					f"[{worker_id}] WARN: Track {track.id} has invalid segmentation type: {type(track.segmentation)}",
					flush=True,
				)
	
	return masks, semantic_ids


def create_corrections_from_descriptions(
	descriptions: List[str],
	semantic_ids: List[int],  
	worker_id: str,
	source_frame_id: Optional[int] = None,
	source_timestamp: Optional[float] = None,
) -> List[ObjectAnnotation]:
	"""Convert descriptions to annotations and retain the exact DAM input frame."""
	corrections = []
	for i, description in enumerate(descriptions):
		if i < len(semantic_ids):
			semantic_id = semantic_ids[i]

			if semantic_id == -1:
				correction = ImageAnnotation(
					semantic_label=description.strip() if description else "unknown",
					confidence=0.0,
				)
			else:
				correction = ObjectAnnotation(
					semantic_id=semantic_id,
					semantic_label=description.strip() if description else "unknown",
					confidence=0.0,
					description_frame_id=source_frame_id,
					description_timestamp=source_timestamp,
				)
			corrections.append(correction)
			print(
				f"[{worker_id}] Created correction for semantic_id {semantic_id}: '{description.strip()}'",
				flush=True,
			)
	return corrections


def correction_label_entry(semantic_id: int, correction: ObjectAnnotation) -> Dict:
	"""Serialize one correction for corrections.yaml without storing large feature vectors."""
	entry = {
		"label": semantic_id,
		"name": correction.semantic_label,
	}
	if correction.description_frame_id is not None:
		entry["description_source"] = {
			"frame_id": correction.description_frame_id,
			"timestamp": correction.description_timestamp,
		}
	if correction.frame_ids:
		entry["temporal_history"] = {
			"frame_ids": correction.frame_ids,
			"timestamps": correction.timestamps,
			"observation_count": correction.observation_count,
			"first_observed": correction.first_observed,
			"last_observed": correction.last_observed,
		}
	if correction.selectframe_clip_feature:
		entry["has_clip_feature"] = True
		entry["clip_feature_dim"] = len(correction.selectframe_clip_feature)
	return entry
