"""Data models for the pipeline orchestrator."""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
import numpy as np
import time

from daaam.tracking.models import Track

@dataclass
class Frame:
	"""Data model for robotics frame observation."""

	# core data
	frame_id: int
	timestamp: float
	rgb_image: np.ndarray  # RGB image
	depth_image: Optional[np.ndarray] = None  # Depth image (optional)
	transform: Optional[np.ndarray] = None  # [x, y, z, qx, qy, qz, qw] (optional, tf2 can fail)

	# derived data (computed from transform history)
	lin_vel: Optional[np.ndarray] = field(default_factory=lambda: np.zeros(3))  # linear velocity [vx, vy, vz]
	ang_vel: Optional[np.ndarray] = field(default_factory=lambda: np.zeros(3))  # angular velocity [wx, wy, wz]

	# camera calibration
	camera_intrinsics: Optional[Dict[str, float]] = None  # {'fx', 'fy', 'cx', 'cy'}

	# updated data (filled by orchestrator during processing)
	tracks: List[Track] = field(default_factory=list)  # List of Track objects


@dataclass
class PromptRecord:
	"""Model for communication between orchestrator and grounding workers."""
	frame: np.ndarray  # RGB image frame
	tracks: List[Track]  # List of Track objects for grounding
	object_labels: Dict[int, int]  # Mapping from track_id to semantic_id
	frame_id: int = -1  # Frame identifier, default -1 if unknown
	timestamp: float = 0.0  # Observation timestamp in seconds


@dataclass
class UQPromptBundle:
	"""UQ-DAAAM: every initial view and refinement candidate of one assignment, so one grounding worker sees all views of an object."""
	views: List[PromptRecord]
	candidates: List[PromptRecord]
	quality: Dict[Any, float]  # (track_id, frame_id) -> q_ij


@dataclass
class UQObjectRecord:
	"""UQ-DAAAM per-object provenance (one uq_objects.jsonl row); travels on the correction queue next to its ObjectAnnotation."""
	data: Dict[str, Any]


@dataclass
class MVRefineJob:
	"""Multi-view refinement: the chosen views of one object (whole frames, referenced, and object-window masks), its first
	description and the observation conditions; kind "fallback" describes the least blurred view of an object that never got
	an admissible frame."""
	semantic_id: int
	track_id: int
	views: List[Dict[str, Any]]  # image (whole frame), mask (window), offset, shape, frame_id, timestamp, q_prime, condition_text
	initial: Optional[Dict[str, Any]]  # frame_id, text, p, gamma, q_prime, condition_text (None for kind "fallback")
	kind: str = "refine"


@dataclass
class MVFlush:
	"""Multi-view end of sequence: describe the aggregated first-description batch now instead of at worker shutdown."""


@dataclass
class MVRecord:
	"""Multi-view result on the correction queue: kind "initial" (p of a first description) or "final" (a refinement)."""
	data: Dict[str, Any]


class MinimalCorrection(BaseModel):
	"""Minimal correction data for assignment workers (without embeddings)."""
	semantic_id: int
	semantic_label: str
	confidence: float
	task_relevance: Optional[List[str]] = None


