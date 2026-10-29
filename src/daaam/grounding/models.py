from typing import List, Dict
from pydantic import BaseModel, Field, field_validator
from dataclasses import dataclass
import numpy as np
from typing import Annotated, Optional
import time


class Annotation(BaseModel):
	"""Base class for annotations."""
	timestamp: float = Field(
		default_factory=time.time,
		description="Timestamp of the image in seconds.",
	)
	semantic_label: str = Field(..., description="Semantic label of the annotation.")
	confidence: Optional[float] = Field(default=0.0, description="Confidence of the label.", ge=0.0, le=10.0)
	embedding: Optional[List[float]] = Field(default=None, description="Embedding vector of the annotation.")

class ObjectAnnotation(Annotation):
	semantic_id: int = Field(
		..., description="Semantic ID of the object displayed in the image."
	)
	description_frame_id: Optional[int] = Field(
		default=None, description="Frame ID of the exact image sent to DAM for this description."
	)
	description_timestamp: Optional[float] = Field(
		default=None, description="Timestamp of the exact image sent to DAM for this description."
	)
	description_mask: Optional[str] = Field(
		default=None, description="Run-relative PNG of the exact mask sent to DAM for this description."
	)
	# Temporal observation history fields
	frame_ids: Optional[List[int]] = Field(
		default=None, description="List of frame IDs where this object was observed."
	)
	timestamps: Optional[List[float]] = Field(
		default=None, description="List of timestamps when this object was observed."
	)
	observation_count: Optional[int] = Field(
		default=None, description="Total number of observations of this object."
	)
	first_observed: Optional[float] = Field(
		default=None, description="Timestamp of first observation."
	)
	last_observed: Optional[float] = Field(
		default=None, description="Timestamp of last observation."
	)
	selectframe_clip_feature: Optional[List[float]] = Field(
		default=None, description="CLIP feature vector for the object patch."
	)
	description_clip_feature: Optional[List[float]] = Field(
		default=None, description="CLIP text feature of the DAM description for online reconciliation."
	)
	
class ImageAnnotation(Annotation):
	"""Classification for an entire image."""
	transform: Optional[List[float]] = Field(
		default=None,
		description="Transformation vector for the image (x, y, z, qx, qy, qz, qw).",
	)
