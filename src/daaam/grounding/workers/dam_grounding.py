import numpy as np
import os
import queue
from typing import Dict, List, Set, Any, Optional, Tuple
from PIL import Image as PILImage
import torch
import traceback
from sentence_transformers import SentenceTransformer
from abc import ABC, abstractmethod
import re
from daaam.pipeline.models import PromptRecord, UQPromptBundle, UQObjectRecord
from daaam.tracking.models import Track
from pathlib import Path
import cv2
import yaml

from daaam.grounding.interfaces import GroundingWorkerInterface
from daaam.utils.embedding import CLIPHandler, SentenceEmbeddingHandler
from daaam.utils.logging import PipelineLogger
from daaam.utils.memory import release_freed_memory
from daaam.utils.performance import performance_measure
from daaam.query_manager.dam import DAMAgentPanoptic
from daaam.grounding.models import ObjectAnnotation, ImageAnnotation
from daaam.grounding.utils import (
	prepare_masks_from_tracks,
	create_corrections_from_descriptions
)


class DAMGroundingWorkerBase(GroundingWorkerInterface):
	"""Base class for DAM grounding workers."""
	
	def _initialize_models(self):
		"""Initialize DAM agent and embedding model."""
		self.worker_logger.info("Initializing DAM agent...")
		dam_model_path = self.config.get("dam_model_path", "nvidia/DAM-3B")
		dam_conv_mode = self.config.get("dam_conv_mode", "v1")
		dam_prompt_mode = self.config.get("dam_prompt_mode", "focal_prompt")
		self.compute_full_image_description = self.config.get("compute_full_image_description", False)

		try:
			self.dam_agent = DAMAgentPanoptic(
				model_path=dam_model_path,
				conv_mode=dam_conv_mode,
				prompt_mode=dam_prompt_mode,
			)
			self.worker_logger.info(f"[Worker {self.worker_id}] DAM agent initialized successfully.")
		except Exception as e:
			self.worker_logger.error(f"[Worker {self.worker_id}] Failed to initialize DAM agent: {e}")
			self.dam_agent = None
		
		# Initialize sentence embedding handler
		sentence_embedding_model_name = self.config.get("sentence_embedding_model_name")
		if sentence_embedding_model_name:
			device = "cuda" if torch.cuda.is_available() else "cpu"
			self.sentence_embedding_handler = SentenceEmbeddingHandler(
				model_name=sentence_embedding_model_name,
				device=device,
				logger=self.worker_logger
			)
			self.worker_logger.info(f"[Worker {self.worker_id}] Initialized sentence embedding: {sentence_embedding_model_name}")
		else:
			self.sentence_embedding_handler = None
			self.worker_logger.info(f"[Worker {self.worker_id}] No sentence embedding model configured")
		
		# Initialize CLIP handler if enabled
		if self.config.get("enable_selectframe_clip_features", False):
			model_name = self.config.get("selectframe_clip_model_name", "ViT-B-16")
			pretrained = self.config.get("selectframe_clip_model_dataset", "openai")
			backend = self.config.get("selectframe_clip_backend", "openclip")

			device = "cuda" if torch.cuda.is_available() else "cpu"
			self.clip_handler = CLIPHandler(
				model_name=model_name,
				device=device,
				pretrained=pretrained,
				backend=backend,
				logger=self.worker_logger
			)

			self.worker_logger.info(f"CLIP model loaded successfully on {device}")
		else:
			self.clip_handler = None

		# Initialize image saving settings
		self.save_grounding_images = self.config.get("save_grounding_images", False)
		self.save_plain_grounding_images = self.config.get("save_plain_grounding_images", False)
		self.save_object_images = self.config.get("save_object_images", False)
		self.output_dir = self.config.get("output_dir", None)
		self.color_map: Dict[int, Tuple[int, int, int]] = self.config.get("color_map", None)
		self.grounding_images_dir = None
		self.grounding_annotations_dir = None
		self.object_images_dir = None
		self.image_counter = 0
		
		if self.save_grounding_images and self.output_dir:
			# Create organized folder structure
			worker_dir = Path(self.output_dir) / f"worker_{self.worker_id}"
			self.grounding_images_dir = worker_dir / "grounding_images"

			if self.save_plain_grounding_images:
				self.grounding_images_dir_plain = worker_dir / "grounding_images_plain"
				self.grounding_images_dir_masks_only = worker_dir / "grounding_images_masks_only"
			self.grounding_annotations_dir = worker_dir / "grounding_annotations"

			self.grounding_images_dir.mkdir(parents=True, exist_ok=True)
			if self.save_plain_grounding_images:
				self.grounding_images_dir_plain.mkdir(parents=True, exist_ok=True)
				self.grounding_images_dir_masks_only.mkdir(parents=True, exist_ok=True)
			self.grounding_annotations_dir.mkdir(parents=True, exist_ok=True)

			if self.save_object_images:
				self.object_images_dir = worker_dir / "object_images"
				self.object_images_dir.mkdir(parents=True, exist_ok=True)

			self.worker_logger.info(f"[Worker {self.worker_id}] Will save grounding data to {worker_dir}")

		# loading the models leaves freed memory resident in this process (about 0.5 GB measured with DAM-3B and the Perception Encoder)
		freed = release_freed_memory()
		self.worker_logger.info(f"[Worker {self.worker_id}] Returned {freed:.0f} MB of freed heap memory to the OS after loading the models.")

		self.worker_logger.info(f"[Worker {self.worker_id}] Finished initializing successfully.")

	def _save_grounding_image(
		self,
		image: np.ndarray,
		masks: List[PILImage.Image],
		semantic_ids: List[int],
		timestamp: Optional[float] = None
	) -> Optional[str]:
		"""Save image with overlaid masks for debugging. Returns base filename."""
		if not self.save_grounding_images or not self.grounding_images_dir:
			return None
			
		try:
			if isinstance(image, PILImage.Image):
				image_np = np.array(image)
			else:
				image_np = image.copy()
			
			overlay = image_np.copy()
			if self.save_plain_grounding_images:
				masks_only = np.zeros(image_np.shape[:2], dtype=np.uint16)
			weight = 0.3

			if self.color_map and np.all([id_ in self.color_map.keys() for id_ in semantic_ids if id_ != -1]):
				self.worker_logger.debug(f"[Worker {self.worker_id}] Using provided color map for grounding visualization.")
			else:
				self.worker_logger.warning(f"[Worker {self.worker_id}] No valid color map provided, using default colors.")
			
			for idx, (mask, sem_id) in enumerate(zip(masks, semantic_ids)):
				if sem_id == -1:  # full image mask
					continue
					
				mask_np = np.array(mask) > 128
				
				if self.color_map and sem_id in self.color_map:
					color = self.color_map[sem_id] 
				else:
					default_colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255), (0, 255, 255)]
					color = default_colors[idx % len(default_colors)]
				
				overlay[mask_np] = overlay[mask_np] * (1 - weight) + np.array(color) * weight

				if self.save_plain_grounding_images:
					masks_only[mask_np] = np.uint16(sem_id)
				
				# semantic ID label
				y, x = np.where(mask_np)
				if len(y) > 0:
					cy, cx = int(np.mean(y)), int(np.mean(x))
					cv2.putText(overlay, f"ID:{sem_id}", (cx-20, cy), 
							   cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
			
			# Generate filename with timestamp (if available) and counter for uniqueness
			if timestamp is not None and timestamp > 0.0:
				base_filename = f"grounding_{timestamp:.6f}_{self.image_counter:06d}"
			else:
				base_filename = f"grounding_{self.image_counter:06d}"
			image_filename = f"{base_filename}.jpg"
			filepath = self.grounding_images_dir / image_filename
			cv2.imwrite(str(filepath), cv2.cvtColor(overlay, cv2.COLOR_RGB2BGR))

			if self.save_plain_grounding_images:
				cv2.imwrite(str(self.grounding_images_dir_plain / image_filename),cv2.cvtColor(image_np, cv2.COLOR_RGB2BGR))
				cv2.imwrite(str(self.grounding_images_dir_masks_only / image_filename.replace(".jpg", ".png")), masks_only.astype(np.uint16))

			self.image_counter += 1

			del overlay
			
			self.worker_logger.debug(f"[Worker {self.worker_id}] Saved grounding image: {filepath}")
			return base_filename
			
		except Exception as e:
			self.worker_logger.error(f"[Worker {self.worker_id}] Failed to save grounding image: {e}")
			return None
	
	def _save_grounding_annotations(
		self,
		base_filename: str,
		semantic_ids: List[int],
		descriptions: List[str],
		timestamp: Optional[float] = None
	) -> None:
		"""Save annotations to YAML file."""
		if not self.save_grounding_images or not self.grounding_annotations_dir or not base_filename:
			return

		try:
			annotations = []
			for sem_id, desc in zip(semantic_ids, descriptions):
				annotations.append({
					"semantic_id": int(sem_id),
					"description": desc,
					"is_full_image": sem_id == -1
				})

			# Create annotation data
			annotation_data = {
				"image_file": f"{base_filename}.jpg",
				"timestamp": timestamp if timestamp is not None else float(self.image_counter - 1),
				"frame_counter": self.image_counter - 1,
				"num_masks": len(semantic_ids),
				"annotations": annotations
			}
			
			# Save to YAML
			yaml_filename = f"{base_filename}.yaml"
			yaml_filepath = self.grounding_annotations_dir / yaml_filename
			
			with open(yaml_filepath, 'w') as f:
				yaml.dump(annotation_data, f, default_flow_style=False, sort_keys=False)
			
			self.worker_logger.debug(f"[Worker {self.worker_id}] Saved annotations: {yaml_filepath}")
			
		except Exception as e:
			self.worker_logger.error(f"[Worker {self.worker_id}] Failed to save annotations: {e}")

	def _save_object_image(self, crop: np.ndarray, semantic_id: int) -> bool:
		"""Save individual object crop for a semantic ID.

		Args:
			crop: RGB image crop as numpy array
			semantic_id: Semantic ID for this object

		Returns:
			True if saved successfully, False otherwise
		"""
		if not self.save_object_images or not self.object_images_dir:
			return False

		# Skip full image masks (semantic_id == -1)
		if semantic_id == -1:
			return False

		try:
			filename = f"{semantic_id:05d}.jpg"
			filepath = self.object_images_dir / filename

			# Convert RGB to BGR for cv2
			crop_bgr = cv2.cvtColor(crop, cv2.COLOR_RGB2BGR)
			cv2.imwrite(str(filepath), crop_bgr)

			self.worker_logger.debug(
				f"[Worker {self.worker_id}] Saved object image: {filepath}"
			)
			return True

		except Exception as e:
			self.worker_logger.error(
				f"[Worker {self.worker_id}] Failed to save object image for "
				f"semantic_id {semantic_id}: {e}"
			)
			return False

	def _extract_clip_features_for_tracks(
		self, 
		frame: np.ndarray, 
		tracks: List[Track], 
		semantic_ids: List[int]
	) -> Dict[int, List[float]]:
		"""Extract CLIP features for each track's patch.
		
		Args:
			frame: RGB image frame
			tracks: List of tracks with bboxes
			semantic_ids: List of semantic IDs corresponding to tracks
			
		Returns:
			Dictionary mapping semantic_id to CLIP feature vector
		"""
		if self.clip_handler is None:
			return {}
			
		try:
			# Collect valid crops and their semantic IDs
			valid_crops = []
			valid_semantic_ids = []
			
			for track, sem_id in zip(tracks, semantic_ids):
				# Skip full image masks
				if sem_id == -1:
					continue
					
				# Get bbox from track
				if hasattr(track, 'bbox') and track.bbox is not None:
					bbox = track.bbox
					if isinstance(bbox, np.ndarray):
						bbox = bbox.tolist()
					
					# Extract crop using bbox (x1, y1, x2, y2)
					x1, y1, x2, y2 = [int(coord) for coord in bbox]
					
					# Ensure bbox is within image bounds
					h, w = frame.shape[:2]
					x1 = max(0, min(x1, w-1))
					x2 = max(x1+1, min(x2, w))
					y1 = max(0, min(y1, h-1))
					y2 = max(y1+1, min(y2, h))
					
					crop = frame[y1:y2, x1:x2]
					
					# Skip if crop is too small
					if crop.shape[0] < 10 or crop.shape[1] < 10:
						self.worker_logger.debug(
							f"[Worker {self.worker_id}] Skipping CLIP feature for semantic_id {sem_id}: "
							f"crop too small ({crop.shape[0]}x{crop.shape[1]})"
						)
						continue
					
					valid_crops.append(crop)
					valid_semantic_ids.append(sem_id)

					# Save object image if enabled
					if self.save_object_images:
						self._save_object_image(crop, sem_id)
				else:
					self.worker_logger.debug(
						f"[Worker {self.worker_id}] Track for semantic_id {sem_id} has no bbox"
					)
			
			if not valid_crops:
				return {}
			
			# Compute CLIP features in batch
			with performance_measure(f"DAMGroundingWorker {self.worker_id}: extract_clip_features", self.worker_logger.debug):
				features_batch = self.clip_handler.extract_image_features_from_arrays(
					valid_crops,
					show_progress=False
				)
			
			# Map features to semantic IDs
			feature_map = {}
			for sem_id, features in zip(valid_semantic_ids, features_batch):
				feature_map[sem_id] = features.tolist()  # Convert to list for JSON serialization
			
			self.worker_logger.info(
				f"[Worker {self.worker_id}] Extracted CLIP features for {len(feature_map)} objects"
			)
			
			return feature_map
			
		except Exception as e:
			self.worker_logger.error(
				f"[Worker {self.worker_id}] Error extracting CLIP features: {e}"
			)
			return {}
	
	@abstractmethod
	def process_prompt_record(self, prompt_record: PromptRecord) -> Optional[List[ObjectAnnotation]]:
		return None


class DAMGroundingWorkerMultiImage(DAMGroundingWorkerBase):
	"""DAM-specific grounding worker that aggregates multiple images/masks for batch processing."""
	
	def __init__(self, query_group_queue, correction_queue, stop_event, config, name=None):
		super().__init__(query_group_queue, correction_queue, stop_event, config, name)
		self.multi_image_min_n_masks = config.get("multi_image_min_n_masks", None)
		
		if self.multi_image_min_n_masks is None:
			self.worker_logger.error("multi_image_min_n_masks not set in config.")
			self.multi_image_min_n_masks = 1  # default to single image behavior
		
		self.aggregated_records = []
		self.total_masks = 0
	
	def process_prompt_record(self, prompt_record: PromptRecord) -> Optional[List[ObjectAnnotation]]:
		"""Process prompt record - aggregate until we have enough masks."""
		if self.dam_agent is None:
			return None

		if isinstance(prompt_record, UQPromptBundle):
			self.aggregated_records.append(prompt_record)
			self.total_masks += sum(len(record.tracks) for record in prompt_record.views)
			if self.total_masks >= self.multi_image_min_n_masks:
				return self._process_aggregated_batch()
			return None
		
		# validate and count masks in record
		prompt_tracks = prompt_record.tracks
		object_labels = prompt_record.object_labels
		
		valid_tracks = [
			t for t in prompt_tracks 
			if object_labels.get(t.id) is not None and 
			   t.segmentation is not None and
			   isinstance(t.segmentation, np.ndarray)
		]
		
		if not valid_tracks:
			self.worker_logger.warning("No valid tracks in record, skipping")
			return None
		
		# add to aggregation
		self.aggregated_records.append(prompt_record)
		self.total_masks += len(valid_tracks)
		
		self.worker_logger.debug(
			f"Aggregated record with {len(valid_tracks)} masks. "
			f"Total: {self.total_masks}/{self.multi_image_min_n_masks}"
		)
		
		# process if enough masks
		if self.total_masks >= self.multi_image_min_n_masks:
			return self._process_aggregated_batch()
		
		# continue aggregating
		return None
	
	def _write_mask(self, relative: str, mask: np.ndarray, enabled: Optional[bool] = None) -> Optional[str]:
		"""Run-relative PNG of an exact mask DAM described (replay overlays it), or None unless enabled (default: save_description_masks)."""
		if enabled is None:
			enabled = self.config.get("save_description_masks", False)
		if not enabled or not self.config.get("output_dir"):
			return None
		path = Path(self.config["output_dir"]) / relative
		path.parent.mkdir(parents=True, exist_ok=True)
		if not cv2.imwrite(str(path), mask.astype(np.uint8) * 255):
			raise OSError(f"could not write {path}")
		return relative

	def _write_uq_view(self, label: int, frame_id: int, frame: np.ndarray, mask: np.ndarray) -> Dict[str, Optional[str]]:
		"""uq.save_views: the exact RGB frame (JPEG, once per frame) and object mask (PNG) of one UQ caption, run-relative, so a
		viewer can show every view without the dataset; both None when disabled."""
		if not self.config["uq"].get("save_views", True) or not self.config.get("output_dir"):
			return {"image_path": None, "mask_path": None}
		image = f"uq_views/frames/{frame_id}.jpg"
		path = Path(self.config["output_dir"]) / image
		if not path.is_file():
			path.parent.mkdir(parents=True, exist_ok=True)
			if not cv2.imwrite(str(path), cv2.cvtColor(np.ascontiguousarray(frame), cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 90]):
				raise OSError(f"could not write {path}")
		return {"image_path": image, "mask_path": self._write_mask(f"uq_views/masks/{label}_{frame_id}.png", np.asarray(mask) > 0, True)}

	def _save_description_mask(self, correction, mask: np.ndarray) -> None:
		if isinstance(correction, ObjectAnnotation):
			correction.description_mask = self._write_mask(f"description_masks/{correction.semantic_id}_{correction.description_frame_id}.png", mask)

	def _attach_text_features(self, corrections: List[ObjectAnnotation]) -> None:
		if os.environ.get("DAAAM_ONLINE_RECONCILIATION") == "1":
			objects = [c for c in corrections if isinstance(c, ObjectAnnotation)]
			if objects:
				if self.clip_handler is None:
					raise RuntimeError("Native reconciliation requires the existing CLIP handler")
				features = self.clip_handler.extract_text_features([c.semantic_label for c in objects], batch_size=8)
				for correction, feature in zip(objects, features):
					correction.description_clip_feature = feature.tolist()
		# Add sentence embeddings if handler is available
		if self.sentence_embedding_handler is not None:
			texts = [c.semantic_label for c in corrections]
			embeddings = self.sentence_embedding_handler.extract_text_embeddings_with_format(
				texts,
				prefix="an object that looks like '",
				postfix="'",
				show_progress=False
			)
			for correction, embedding in zip(corrections, embeddings):
				correction.embedding = embedding.tolist()

	def _process_aggregated_batch(self) -> Optional[List[ObjectAnnotation]]:
		"""Process the aggregated batch of records."""
		if not self.aggregated_records:
			return None
		if isinstance(self.aggregated_records[0], UQPromptBundle):
			try:
				return self._process_uq_batch(self.aggregated_records)
			except Exception as e:
				self.worker_logger.error(f"[Worker {self.worker_id}] ERROR processing UQ batch: {e}")
				self.worker_logger.error(traceback.format_exc())
				return None
			finally:
				self.aggregated_records = []
				self.total_masks = 0
				if torch.cuda.is_available():
					torch.cuda.empty_cache()
		
		self.worker_logger.info(
			f"Processing batch of {len(self.aggregated_records)} records "
			f"with {self.total_masks} total masks"
		)
		
		try:
			# prep image-mask pairs for multi-image processing
			image_mask_pairs = []
			all_semantic_ids_by_record = []
			all_clip_features_by_record = []  # Store CLIP features for each record
			all_frame_ids_by_record = []  # Exact selected frame used for each DAM image
			all_timestamps_by_record = []  # Store timestamps for each record
			base_filenames = []

			for record in self.aggregated_records:
				prompt_frame = record.frame
				prompt_tracks = record.tracks
				object_labels = record.object_labels
				record_timestamp = record.timestamp
				record_frame_id = record.frame_id
				
				pil_frame = PILImage.fromarray(prompt_frame)
				
				masks, semantic_ids = prepare_masks_from_tracks(
					prompt_tracks, object_labels, self.worker_id
				)

				if not masks:
					self.worker_logger.error(f"[Worker {self.worker_id}] No valid masks to process.")
					return None

				# Extract CLIP features if enabled
				clip_features = {}
				if self.config.get("enable_selectframe_clip_features", False) and self.clip_handler is not None:
					# Filter tracks to match semantic_ids (excluding -1 for full image)
					valid_tracks = [t for t, sid in zip(prompt_tracks, semantic_ids[:len(prompt_tracks)]) if sid != -1]
					valid_semantic_ids = [sid for sid in semantic_ids[:len(prompt_tracks)] if sid != -1]
					
					if valid_tracks:
						clip_features = self._extract_clip_features_for_tracks(
							prompt_frame, valid_tracks, valid_semantic_ids
						)

				# Add full image mask if enabled
				if self.compute_full_image_description:
					full_image_mask = PILImage.fromarray((np.ones_like(masks[0]) * 255).astype(np.uint8), mode='L')
					masks.append(full_image_mask)
					semantic_ids.append(-1)  # Special semantic_id for full image
					self.worker_logger.debug(f"[Worker {self.worker_id}] Added full image mask for image {len(image_mask_pairs)}")
				
				if masks:
					image_mask_pairs.append((pil_frame, masks))
					all_semantic_ids_by_record.append(semantic_ids)
					all_clip_features_by_record.append(clip_features)
					all_frame_ids_by_record.append(record_frame_id)
					all_timestamps_by_record.append(record_timestamp)

					# Save grounding image if enabled
					base_filename = self._save_grounding_image(prompt_frame, masks, semantic_ids, timestamp=record_timestamp)
					base_filenames.append(base_filename)
			
			if not image_mask_pairs:
				self.worker_logger.error(f"[Worker {self.worker_id}] No valid image-mask pairs to process.")
				return None
			
			total_masks = sum(len(masks) for _, masks in image_mask_pairs)
			self.worker_logger.info(
				f"[Worker {self.worker_id}] Querying DAM agent with {len(image_mask_pairs)} images "
				f"and {total_masks} total masks..."
			)
			
			# DAM Agent
			# Use smaller max_new_tokens if full image description is included to prevent repetition
			max_tokens = 196 if self.compute_full_image_description else 512
			with performance_measure(f"DAMGroundingWorker {self.worker_id}: dam_agent.query_multi_image_multi_mask", self.worker_logger.debug):
				descriptions_by_image = self.dam_agent.query_multi_image_multi_mask(
					image_mask_pairs=image_mask_pairs,
					query="Describe what you see in this region.",
					temperature=0.2, 
					top_p=0.9,
					max_new_tokens=max_tokens
				)
			
			self.worker_logger.info(
				f"[Worker {self.worker_id}] DAM agent returned descriptions for {len(descriptions_by_image)} images"
			)
			
			# ObjectAnnotation objects
			corrections = []
			for img_idx, descriptions in enumerate(descriptions_by_image):
				if img_idx < len(all_semantic_ids_by_record):
					semantic_ids = all_semantic_ids_by_record[img_idx]
					clip_features = all_clip_features_by_record[img_idx] if img_idx < len(all_clip_features_by_record) else {}
					record_frame_id = all_frame_ids_by_record[img_idx] if img_idx < len(all_frame_ids_by_record) else None
					record_timestamp = all_timestamps_by_record[img_idx] if img_idx < len(all_timestamps_by_record) else None

					# Save annotations if enabled
					if img_idx < len(base_filenames) and base_filenames[img_idx] and descriptions:
						self._save_grounding_annotations(base_filenames[img_idx], semantic_ids, descriptions, timestamp=record_timestamp)

					# Create corrections and attach CLIP features
					record_corrections = create_corrections_from_descriptions(
						descriptions,
						semantic_ids,
						self.worker_id,
						source_frame_id=record_frame_id,
						source_timestamp=record_timestamp,
					)
					
					for correction, mask in zip(record_corrections, image_mask_pairs[img_idx][1]):
						self._save_description_mask(correction, np.array(mask) > 0)

					# Attach CLIP features to corrections
					for correction in record_corrections:
						if hasattr(correction, 'semantic_id') and correction.semantic_id in clip_features:
							correction.selectframe_clip_feature = clip_features[correction.semantic_id]
							self.worker_logger.debug(
								f"[Worker {self.worker_id}] Attached CLIP feature to semantic_id {correction.semantic_id}"
							)
					
					corrections.extend(record_corrections)

			# Clear GPU cache after processing all images
			if torch.cuda.is_available():
				torch.cuda.empty_cache()
			
			if corrections:
				self._attach_text_features(corrections)
				return corrections
			else:
				self.worker_logger.warning(f"[Worker {self.worker_id}] Received no valid corrections from DAM agent.")
				return None
			
		except Exception as e:
			self.worker_logger.error(f"[Worker {self.worker_id}] ERROR processing multi-image batch: {e}")

			self.worker_logger.error(traceback.format_exc())
			return None
		finally:
			# clear batch after processing
			self.aggregated_records = []
			self.total_masks = 0
			
			# clear GPU cache after batch processing
			if torch.cuda.is_available():
				torch.cuda.empty_cache()
				self.worker_logger.debug(f"[Worker {self.worker_id}] Cleared GPU cache after multi-image batch processing")
	
	def _uq_encode(self, texts: List[str]) -> np.ndarray:
		"""Caption embeddings phi(c): "clip" reuses the loaded CLIP text encoder, otherwise a locally cached sentence-transformers model."""
		name = self.config["uq"]["text_encoder"]
		if name == "clip":
			if self.clip_handler is None:
				raise RuntimeError("uq.text_encoder=clip needs enable_selectframe_clip_features (the already loaded CLIP model)")
			return np.asarray(self.clip_handler.extract_text_features(texts, batch_size=8))
		if getattr(self, "_uq_encoder", None) is None:
			self._uq_encoder = SentenceTransformer(name, device="cuda" if torch.cuda.is_available() else "cpu", local_files_only=True)
		return np.asarray(self._uq_encoder.encode(texts, batch_size=8, show_progress_bar=False))

	def _uq_describe(self, requests: List[Tuple[PromptRecord, Track]]) -> List[Tuple[str, Optional[float]]]:
		"""One scored DAM query for (record, track) requests, grouped per source frame; returns (text, mean token probability)."""
		groups = {}
		for index, (record, track) in enumerate(requests):
			groups.setdefault(id(record), (record, []))[1].append((index, track))
		pairs = [
			(PILImage.fromarray(record.frame), [PILImage.fromarray((track.segmentation * 255).astype(np.uint8), mode="L") for _, track in items])
			for record, items in groups.values()
		]
		with performance_measure(f"DAMGroundingWorker {self.worker_id}: uq scored query", self.worker_logger.debug):
			descriptions, probabilities = self.dam_agent.query_multi_image_multi_mask_scored(
				pairs,
				query="Describe what you see in this region.",
				temperature=self.config["uq"]["caption_temperature"],
				top_p=0.9,
				max_new_tokens=196 if self.compute_full_image_description else 512,
			)
		results = [None] * len(requests)
		for (_, items), texts, probs in zip(groups.values(), descriptions, probabilities):
			for (index, _), text, probability in zip(items, texts, probs):
				results[index] = ((text or "").strip(), probability)
		return results

	def _uq_completion(self):
		"""Fusion LLM call, or None without a key; the key comes from the environment/.env exactly as for the replay agent."""
		from daaam.uq.fusion import openai_complete

		uq = self.config["uq"]
		if not uq.get("fusion_enabled", True):
			return None
		if not hasattr(self, "_uq_complete"):
			self._uq_complete = openai_complete(uq["fusion_model"])
		return self._uq_complete

	def _process_uq_batch(self, bundles: List[UQPromptBundle]) -> Optional[List[Any]]:
		"""UQ-DAAAM: r initial captions -> u_j; refine above tau by descending q within budget B; fuse; one correction per object."""
		import hashlib
		from daaam.uq.refinement import finish_objects, refine_objects

		uq = self.config["uq"]
		digest = lambda array: hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()
		sources, objects = {}, {}
		for bundle in bundles:
			for record in bundle.views + bundle.candidates:
				for track in record.tracks:
					if record.object_labels.get(track.id) is not None and isinstance(track.segmentation, np.ndarray):
						sources[track.id, record.frame_id] = (record, track)
			for record in bundle.views:
				for track in record.tracks:
					if (track.id, record.frame_id) in sources:
						item = objects.setdefault(track.id, {"semantic_id": int(record.object_labels[track.id]), "initial": [], "candidates": {}})
						item["initial"].append(record.frame_id)
			for (track_id, frame_id), q in bundle.quality.items():
				if track_id in objects and frame_id not in objects[track_id]["initial"] and (track_id, frame_id) in sources:
					objects[track_id]["candidates"][frame_id] = q
		if not objects:
			self.worker_logger.warning("UQ batch has no describable tracks")
			return None
		quality = {key: q for bundle in bundles for key, q in bundle.quality.items()}
		frame_hashes = {}

		def describe(stage, keys):
			"""Describe (track_id, frame_id) keys and return view provenance dicts with embeddings."""
			results = self._uq_describe([sources[key] for key in keys])
			embeddings = self._uq_encode([text for text, _ in results])
			views = []
			for key, (text, probability), embedding in zip(keys, results, embeddings):
				record, track = sources[key]
				if id(record) not in frame_hashes:
					frame_hashes[id(record)] = digest(record.frame)
				views.append(dict(stage=stage, frame_id=key[1], quality=quality.get(key, 0.0), probability=probability,
					text=text, embedding=embedding, timestamp=record.timestamp, rgb_sha256=frame_hashes[id(record)],
					mask_sha256=digest(track.segmentation),
					**self._write_uq_view(record.object_labels[track.id], key[1], record.frame, track.segmentation)))
			return views

		if not hasattr(self, "_uq_seen"):
			self._uq_seen = []  # initial u of every object this worker has seen: causal quantile threshold
		tau = refine_objects(objects, describe, uq, self._uq_seen)
		finished = finish_objects(objects, tau, uq, self._uq_completion())

		corrections, rows, best_views = [], [], {}
		for track_id, item in objects.items():
			row = dict(semantic_id=item["semantic_id"], track_id=int(track_id), **finished[track_id], worker=self.worker_id)
			best = next(view for view in row["views"] if view["frame_id"] == row["description_frame_id"])
			best_views[track_id] = best["frame_id"]
			corrections.append(ObjectAnnotation(
				semantic_id=item["semantic_id"], semantic_label=row["final_caption"], confidence=0.0,
				description_frame_id=best["frame_id"], description_timestamp=best["timestamp"]))
			self._save_description_mask(corrections[-1], np.asarray(sources[track_id, best["frame_id"]][1].segmentation) > 0)
			rows.append(row)

		if self.config.get("enable_selectframe_clip_features", False) and self.clip_handler is not None:
			for correction, track_id in zip(corrections, objects):
				record, track = sources[track_id, best_views[track_id]]
				feature = self._extract_clip_features_for_tracks(record.frame, [track], [correction.semantic_id])
				if correction.semantic_id in feature:
					correction.selectframe_clip_feature = feature[correction.semantic_id]
		self._attach_text_features(corrections)
		self.worker_logger.info(
			f"[Worker {self.worker_id}] UQ batch: {len(objects)} objects, tau={tau}, "
			f"refined={sum(any(v['stage'] == 'refine' for v in row['views']) for row in rows)}")
		output = []
		for correction, row in zip(corrections, rows):
			output += [correction, UQObjectRecord(row)]
		return output

	def run(self):
		"""Override run to handle remaining records when stopping."""
		if self.dam_agent is None:
			self.worker_logger.error("DAM agent not initialized. Exiting worker.")
			return
		
		super().run()

		if self.config.get("uq", {}).get("enabled", False):
			# describe bundles already delivered instead of dropping them at shutdown (upstream exits on stop_event)
			while True:
				try:
					item = self.input_queue.get(timeout=1.0)
				except queue.Empty:
					break
				for correction in self.process_item(item) or []:
					self.output_queue.put(correction)
		
		# remaining records before shutting down
		if self.aggregated_records and self.total_masks > 0:
			self.worker_logger.info(
				f"Processing final batch of {len(self.aggregated_records)} records "
				f"with {self.total_masks} total masks"
			)
			corrections = self._process_aggregated_batch()
			if corrections:
				# to out queue
				self.worker_logger.info(f"[Worker {self.worker_id}] Putting {len(corrections)} corrections on output queue")
				for correction in corrections:
					self.output_queue.put(correction)
			else:
				self.worker_logger.warning(f"[Worker {self.worker_id}] No valid corrections to put on output queue.")
