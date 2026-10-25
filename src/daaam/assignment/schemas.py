"""Serialization and validation schemas for assignment service."""
from typing import Dict, Any, List
import json
import numpy as np

from daaam.tracking.models import SimplifiedTrack
from daaam.assignment.models import AssignmentTask, SelectedGroup

from daaam.grounding.schemas import annotation_from_dict, annotation_to_json_serializable

def assignment_task_to_json(task: AssignmentTask) -> str:
	"""Convert AssignmentTask to JSON string for worker communication."""
	from daaam.pipeline.models import MinimalCorrection
	assert isinstance(task, AssignmentTask), "task must be an instance of AssignmentTask"

	data = {
		"track_history": [
			[
				{
					"id": track.id,
					"bbox": track.bbox.tolist() if isinstance(track.bbox, np.ndarray) else track.bbox,
					"depth_valid": track.depth_valid,
					"region_area": track.region_area,
					"median_depth": track.median_depth,
					"lin_vel": track.lin_vel.tolist() if isinstance(track.lin_vel, np.ndarray) else track.lin_vel,
					"ang_vel": track.ang_vel.tolist() if isinstance(track.ang_vel, np.ndarray) else track.ang_vel,
				}
				for track in frame_data
			]
			for frame_data in task.track_history
		],
		"frame_dims": task.frame_dims,
		"object_labels": task.object_labels,
		"corrections": {
			idx: correction.model_dump() if isinstance(correction, MinimalCorrection)
			else annotation_to_json_serializable(correction)
			for idx, correction in task.corrections.items()
		},
		"prompted_track_ids": task.prompted_track_ids,
		"start_frame_count": task.start_frame_count
	}
	if task.assignment_id is not None:
		data["assignment_id"] = task.assignment_id
		data["frame_id_mapping"] = task.frame_id_mapping
	return json.dumps(data)


def json_to_assignment_task(json_str: str) -> AssignmentTask:
	"""Convert JSON string back to AssignmentTask."""
	from daaam.pipeline.models import MinimalCorrection
	data = json.loads(json_str)

	# Reconstruct track history with SimplifiedTrack objects
	track_history = []
	for frame_data in data["track_history"]:
		tracks = [
			SimplifiedTrack(
				id=track["id"],
				bbox=np.array(track["bbox"]),
				depth_valid=track.get("depth_valid", True),
				region_area=track.get("region_area", 0),
				median_depth=track.get("median_depth", 0.0),
				lin_vel=np.array(track.get("lin_vel", [0, 0, 0])),
				ang_vel=np.array(track.get("ang_vel", [0, 0, 0])),
			)
			for track in frame_data
		]
		track_history.append(tracks)

	return AssignmentTask(
		track_history=track_history,
		frame_dims=tuple(data["frame_dims"]),
		object_labels=data["object_labels"],
		corrections={
			idx: MinimalCorrection(**correction) if 'embedding' not in correction
			else annotation_from_dict(correction)
			for idx, correction in data["corrections"].items()
		},
		prompted_track_ids=data["prompted_track_ids"],
		start_frame_count=data["start_frame_count"],
		assignment_id=data.get("assignment_id"),
		frame_id_mapping={int(k): int(v) for k, v in data["frame_id_mapping"].items()}
			if data.get("frame_id_mapping") is not None else None,
	)


def selected_group_to_dict(group: SelectedGroup) -> Dict[str, Any]:
	"""Convert SelectedGroup to dictionary."""
	return group.__dict__


def dict_to_selected_group(data: Dict[str, Any]) -> SelectedGroup:
	"""Convert dictionary to SelectedGroup."""
	return SelectedGroup(
		frame_idx=data["frame_idx"],
		track_ids=data["track_ids"],
		start_frame_count=data["start_frame_count"]
	)
