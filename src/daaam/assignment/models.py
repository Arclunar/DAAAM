"""Data models for the assignment service."""
from dataclasses import dataclass
from typing import List, Dict, Any, Union, Optional
import numpy as np

from daaam.tracking.models import SimplifiedTrack
from daaam.grounding.models import ObjectAnnotation

@dataclass 
class AssignmentTask:
	"""Task data sent to assignment workers."""
	track_history: List[List[SimplifiedTrack]]
	frame_dims: tuple
	object_labels: Dict[int, int]
	corrections: Dict[int, ObjectAnnotation]
	prompted_track_ids: List[int]
	start_frame_count: int
	frame_id_mapping: Dict[int, int] = None  # local_idx -> global_frame_id
	assignment_id: Optional[int] = None


@dataclass
class SelectedGroup:
	"""
	Group of tracks selected for grounding.
	
	Attributes:
		global_frame_id: int	# Global frame ID (absolute, not relative)
		track_ids: List[int]	# IDs of the tracks in this group
	"""
	global_frame_id: int
	track_ids: List[int]
	role: str = "view"  # UQ-DAAAM: "view" is described now, "candidate" is kept for active refinement
	quality: Optional[Dict[int, float]] = None  # UQ-DAAAM: track_id -> q_ij in this frame


@dataclass
class AssignmentResult:
	"""One terminal result, including empty selections and worker failures."""
	assignment_id: int
	groups: List[SelectedGroup]
	status: str = "completed"
	error: Optional[str] = None
