import requests
import base64
from io import BytesIO
from PIL import Image
from typing import List, Union, Optional, Dict, Any, Tuple, Literal
import subprocess
import time
import os
import sys
from pathlib import Path
import signal
import numpy as np
from urllib.parse import urlparse
import shlex
import threading
import gradio as gr
import torch
import io
from openai import OpenAI
import multiprocessing as mp
import requests
import json

# avoid CUDA issues
if mp.get_start_method(allow_none=True) != 'spawn':
    try:
        mp.set_start_method('spawn', force=True)
    except RuntimeError:
        # If already set, ignore the error
        pass

from daaam.query_manager.mmllm import Agent

from dam import DescribeAnythingModel, disable_torch_init

import sys
from pathlib import Path


class DAMAgentPanoptic:
    """
    A high-performance DAM agent that directly uses the model for panoptic queries
    without server overhead. Handles RGB images and separate masks efficiently.
    """
    
    def __init__(
        self,
        model_path = "nvidia/DAM-3B",
        conv_mode = "v1",
        prompt_mode="focal_prompt",
        **kwargs
    ):
        """
        Initialize the direct DAM agent for panoptic queries.
        
        Args:
            model_path: Path to the model checkpoint or model name (huggingface)
            conv_mode: Conversation mode (e.g., "v1")
            prompt_mode: Prompt mode (e.g., "focal_prompt")
            **kwargs: Additional arguments for model loading
        """
        self.model_path = model_path
        self.conv_mode = conv_mode
        self.prompt_mode = prompt_mode
        
        try:
            
            disable_torch_init()
            self.dam_model = DescribeAnythingModel(
                model_path=model_path,
                conv_mode=conv_mode,
                prompt_mode=f"full+{prompt_mode.replace('_prompt', '_crop')}" if "prompt" in prompt_mode else prompt_mode,
                **kwargs
            )
            print(f"DAM model {self.dam_model.model_name} loaded successfully.")
            
        except ImportError as e:
            raise ImportError(f"Could not import DAM model directly. Make sure the describe-anything package is available: {e}")
    
    def query_panoptic(
        self, 
        image: Image.Image, 
        masks: List[Image.Image], 
        query: str = "Describe the masked region in detail.",
        batch_size: Optional[int] = None,
        temperature: float = 0.2,
        top_p: float = 0.9,
        num_beams: int = 1,
        max_new_tokens: int = 512
    ) -> List[str]:
        """
        Query the DAM model directly with RGB image and separate masks for panoptic segmentation.
        
        Args:
            image: RGB PIL Image as the base image
            masks: List of grayscale mask PIL Images
            query: Query text for description
            batch_size: Optional batch size for processing masks
            temperature: Sampling temperature
            top_p: Nucleus sampling probability
            num_beams: Number of beams for beam search
            max_new_tokens: Maximum number of tokens to generate
            
        Returns:
            List of description strings, one for each mask
        """
        if not masks:
            return []
        
        if image.mode != 'RGB':
            image = image.convert('RGB')
        
        processed_masks = []
        for mask in masks:
            if mask.mode != 'L':
                mask = mask.convert('L')
            processed_masks.append(mask)
        
        if "<image>" not in query:
            query = f"<image>\n{query}"
        
        descriptions = self.dam_model.get_panoptic_descriptions(
            image_pil=image,
            mask_pils=processed_masks,
            query=query,
            streaming=False,
            temperature=temperature,
            top_p=top_p,
            num_beams=num_beams,
            max_new_tokens=max_new_tokens,
            batch_size=batch_size
        )
        
        for i, desc in enumerate(descriptions):
            print(f"[DAM Panoptic Agent Response - Mask {i}]: {desc}")
        
        return descriptions
    
    def query_single(
        self,
        image: Image.Image,
        mask: Image.Image,
        query: str = "Describe the masked region in detail.",
        temperature: float = 0.2,
        top_p: float = 0.9,
        num_beams: int = 1,
        max_new_tokens: int = 512
    ) -> str:
        """
        Query a single mask on an image.
        
        Args:
            image: RGB PIL Image
            mask: Grayscale mask PIL Image
            query: Query text for description
            temperature: Sampling temperature
            top_p: Nucleus sampling probability
            num_beams: Number of beams for beam search
            max_new_tokens: Maximum number of tokens to generate
            
        Returns:
            Description string
        """
        descriptions = self.query_panoptic(
            image=image,
            masks=[mask],
            query=query,
            temperature=temperature,
            top_p=top_p,
            num_beams=num_beams,
            max_new_tokens=max_new_tokens
        )
        return descriptions[0] if descriptions else ""

    def query_multi_image_multi_mask(
        self,
        image_mask_pairs: List[Tuple[Image.Image, List[Image.Image]]],
        query: str = "Describe what you see in this region.",
        batch_size: Optional[int] = None,
        auto_batch: bool = True,
        temperature: float = 0.2,
        top_p: float = 0.9,
        num_beams: int = 1,
        max_new_tokens: int = 512
    ) -> List[List[str]]:
        """
        Query multiple images with multiple masks efficiently using DAM.
        
        Args:
            image_mask_pairs: List of tuples (image, list_of_masks) where each image is RGB PIL Image
                             and masks are grayscale PIL Images
            query: Query text for description
            batch_size: Optional batch size for processing
            auto_batch: Whether to use intelligent batching for same-sized images
            temperature: Sampling temperature
            top_p: Nucleus sampling probability
            num_beams: Number of beams for beam search
            max_new_tokens: Maximum number of tokens to generate
            
        Returns:
            List of lists of description strings. Outer list corresponds to images,
            inner lists correspond to masks for each image.
        """
        if not image_mask_pairs:
            return []
        
        processed_pairs = []
        for image, masks in image_mask_pairs:
            if image.mode != 'RGB':
                image = image.convert('RGB')
            
            processed_masks = []
            for mask in masks:
                if mask.mode != 'L':
                    mask = mask.convert('L')
                processed_masks.append(mask)
            
            processed_pairs.append((image, processed_masks))
        
        if "<image>" not in query:
            query = f"<image>\n{query}"
        
        # custom multi-image multi-mask dam prompting
        descriptions = self.dam_model.get_multi_image_descriptions(
            image_mask_pairs=processed_pairs,
            query=query,
            streaming=False,
            batch_size=batch_size,
            auto_batch=auto_batch,
            temperature=temperature,
            top_p=top_p,
            num_beams=num_beams,
            max_new_tokens=max_new_tokens
        )
        
        # debug prints
        total_masks = sum(len(masks) for _, masks in processed_pairs)
        print(f"[DAM Multi-Image Agent] Processed {len(processed_pairs)} images with {total_masks} total masks")
        for img_idx, image_descriptions in enumerate(descriptions):
            for mask_idx, desc in enumerate(image_descriptions):
                print(f"[DAM Multi-Image Agent Response - Image {img_idx}, Mask {mask_idx}]: {desc}")
        
        return descriptions
    def query_multi_image_multi_mask_scored(
        self,
        image_mask_pairs: List[Tuple[Image.Image, List[Image.Image]]],
        **kwargs
    ) -> Tuple[List[List[str]], List[List[Optional[float]]]]:
        """UQ-DAAAM: descriptions plus the mean model probability of each sampled non-special token (None if it has none).

        The installed DAM package is not modified: model.generate is wrapped for this call only and gets a logits processor
        that runs before the temperature/top-p warpers, i.e. it reads the raw next-token distribution. auto_batch=False keeps
        the generation order equal to the flattened (image, mask) order used to regroup the probabilities.
        """
        from daaam.uq.scoring import scored

        descriptions, flat = scored(self.dam_model, lambda: self.query_multi_image_multi_mask(image_mask_pairs, auto_batch=False, **kwargs))
        if len(flat) != sum(len(masks) for _, masks in image_mask_pairs):
            raise RuntimeError("token probabilities do not match the generated descriptions")
        probabilities, start = [], 0
        for _, masks in image_mask_pairs:
            probabilities.append(flat[start:start + len(masks)])
            start += len(masks)
        return descriptions, probabilities
