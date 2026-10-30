"""Data models for the tracking service."""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
import numpy as np
import time
import cv2
import weakref

@dataclass
class SimplifiedTrack:
	"""Simplified track data for assignment workers (no segmentation mask)."""
	id: int
	bbox: np.ndarray  # JSON-serializable list
	depth_valid: bool = True
	region_area: int = 0

	median_depth: float = 0.0
	lin_vel: np.ndarray = field(default_factory=lambda: np.zeros(3))
	ang_vel: np.ndarray = field(default_factory=lambda: np.zeros(3))
	quality_weight: float = 1.0  # multiview motion-blur weight w: the BLP maximizes q' = q * w (1.0 = upstream q)


@dataclass
class Track:
	"""Track data with polygon-based segmentation storage for memory efficiency."""
	id: int
	segmentation_contours: List[np.ndarray]  # Polygon contours instead of full mask
	bbox: np.ndarray
	image_shape: Tuple[int, int]  # Store shape for mask reconstruction
	depth_valid: bool = True  # Whether depth is valid for this track
	region_area: int = 0  # Area of the segmented region

	median_depth: float = 0.0
	lin_vel: np.ndarray = field(default_factory=lambda: np.zeros(3))
	ang_vel: np.ndarray = field(default_factory=lambda: np.zeros(3))

	# Cache for reconstructed mask (not included in repr or init)
	_mask_cache: Optional[np.ndarray] = field(default=None, init=False, repr=False)
	_cache_ref: Optional[weakref.ReferenceType] = field(default=None, init=False, repr=False)

	def get_mask(self) -> np.ndarray:
		"""Reconstruct mask from polygon contours (lazy evaluation with caching)."""
		# Check if we have a valid cached mask
		if self._mask_cache is not None:
			return self._mask_cache

		# Reconstruct mask from polygons
		mask = np.zeros(self.image_shape, dtype=np.uint8)
		if self.segmentation_contours:  # Check if contours exist
			cv2.fillPoly(mask, self.segmentation_contours, 1)

		# Cache the reconstructed mask
		self._mask_cache = mask.astype(bool)
		return self._mask_cache

	@property
	def segmentation(self) -> np.ndarray:
		"""Backward compatibility property for accessing segmentation mask."""
		return self.get_mask()

	def clear_cache(self) -> None:
		"""Clear the cached mask to free memory."""
		self._mask_cache = None
		self._cache_ref = None

	def get_simplified(self) -> SimplifiedTrack:
		"""Get simplified track data for JSON serialization."""
		return SimplifiedTrack(
			id=self.id,
			bbox=self.bbox,
			depth_valid=self.depth_valid,
			region_area=self.region_area,
			median_depth=self.median_depth,
			lin_vel=self.lin_vel,
			ang_vel=self.ang_vel,
		)

	def __post_init__(self):
		# Ensure bbox is a numpy array
		if not isinstance(self.bbox, np.ndarray):
			self.bbox = np.array(self.bbox)

		# Ensure contours are numpy arrays
		if self.segmentation_contours:
			self.segmentation_contours = [
				np.array(c) if not isinstance(c, np.ndarray) else c
				for c in self.segmentation_contours
			]

		# If region_area not provided, compute from contours
		if self.region_area == 0 and self.segmentation_contours:
			from daaam.utils.vision import compute_polygon_area
			self.region_area = compute_polygon_area(self.segmentation_contours)

	@classmethod
	def from_mask(cls, id: int, mask: np.ndarray, bbox: np.ndarray,
	              epsilon_factor: float = 0.001, **kwargs) -> "Track":
		"""Create Track from a segmentation mask by converting to polygons.

		Args:
			id: Track ID
			mask: Boolean segmentation mask
			bbox: Bounding box coordinates
			epsilon_factor: Polygon approximation factor (0 = no approximation)
			**kwargs: Additional Track fields (depth_valid, median_depth, etc.)

		Returns:
			Track instance with polygon-based segmentation
		"""
		from daaam.utils.vision import mask_to_polygons, compute_polygon_area

		# Convert mask to polygons
		contours = mask_to_polygons(mask, epsilon_factor)

		# Compute area from polygons
		region_area = compute_polygon_area(contours)

		return cls(
			id=id,
			segmentation_contours=contours,
			bbox=bbox,
			image_shape=mask.shape,
			region_area=region_area,
			**kwargs
		)