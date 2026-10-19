#!/usr/bin/env python3
"""Main script to run the daaam pipeline without ROS.

This script provides a command-line interface to process datasets through
the DAAAM pipeline with Hydra integration for scene graph generation.

Usage:
	python run_pipeline.py /path/to/dataset --config config/pipeline_config.yaml
	python run_pipeline.py /path/to/dataset --hydra-config habitat --target-fps 10

====================================================================
【本脚本在 DAAAM 系统中的角色】(中文导读)

DAAAM 是一套实时的「基础模型优先」机器人建图系统，数据流为:

	RGB 图像
	  → 分割 (SAM / FastSAM / SAM2)   ← SegmentationService
	  → 跟踪 (BotSort)                ← TrackingService
	  → 帧选择与任务分配               ← AssignmentService
	  → 语义描述 (DAM / 多模态 LLM)    ← GroundingService
	  → 修正 Hydra 3D 动态场景图       ← SceneGraphService

本脚本是「不依赖 ROS 的离线入口」：
它只负责 组装配置 → 加载数据集 → 创建 HydraPipelineRunner → 运行。
真正的逐帧处理逻辑在 src/daaam/pipeline/orchestrator.py 的
PipelineOrchestrator 中（由 HydraPipelineRunner 在内部实例化）。

main() 的执行流程概览：
	1. parse_args()                       解析命令行参数
	2. PipelineConfig.from_yaml()         加载 YAML 配置
	3. apply_config_overrides()           应用 --config-overrides 的点号路径覆盖
	4. 实例化数据集                         ImageSequenceDataset / HM3DSemDataset / CodaDataset
	5. 创建 HydraPipelineRunner           内部启动所有服务与 worker 进程
	6. runner.run() 逐帧处理              输出场景图修正、统计信息与可视化图片
====================================================================
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Optional
import json
import numpy as np

# 把仓库根目录加入 sys.path，使 `from daaam import ...` 可用。
# daaam 包位于 src/daaam/（由 pyproject.toml 声明，但离线脚本直接运行时需要手动加路径）。
sys.path.insert(0, str(Path(__file__).parent.parent))

from daaam.config import PipelineConfig  # 全管道配置（见 src/daaam/config.py）
from daaam.datasets import ImageSequenceDataset, HM3DSemDataset, CodaDataset, LiveDataset  # 三种离线数据集 + 实时数据源（见 --dataset-type LiveDataset）
from daaam.hydra.runner import HydraPipelineRunner  # 封装 PipelineOrchestrator 的离线运行器
from daaam import ROOT_DIR  # 仓库根目录绝对路径
from daaam.utils.hf_cache import resolve_cached_hf_model  # 缓存里有模型时返回本地快照目录（离线可用）


def parse_args():
	"""Parse command line arguments."""
	# 设计约定：几乎所有参数的默认值都是 None，意思是「不覆盖 YAML 配置」。
	# 真正的默认值全部来自 config/pipeline_config.yaml，CLI 只做显式覆盖。
	parser = argparse.ArgumentParser(
		description="Run daaam pipeline on datasets without ROS"
	)

	# Required arguments
	# --- 必需参数 ---
	parser.add_argument(
		"data_path",
		type=str,
		help="Path to dataset (folder for image sequences, file for rosbags)"
	)
	# 位置参数：数据集路径。可以是图像序列文件夹，也可以是 rosbag 文件。

	# Configuration
	# --- 配置相关 ---
	parser.add_argument(
		"--config",
		type=str,
		default=None,
		help="Path to pipeline configuration YAML file"
	)
	# 主配置文件路径（pipeline_config.yaml）。相对路径会基于 ROOT_DIR 解析（见 main）。

	parser.add_argument(
		"--config-overrides",
		type=str,
		nargs='+',
		help="Configuration overrides in key=value format (e.g., workers.num_grounding_workers=8)"
	)
	# 以「点号路径 key=value」覆盖任意嵌套配置项，如 workers.num_grounding_workers=8。
	# 优先级最高，在 main 的最后一步应用，可覆盖前面所有来源设置的任何字段。

	# Dataset options
	# --- 数据集选项 ---
	parser.add_argument(
		"--dataset-type",
		type=str,
		choices=["ImageSequenceDataset", "HM3DSemDataset", "CodaDataset", "LiveDataset"],
		default="ImageSequenceDataset",
		help="Dataset class name to use (LiveDataset: frames pulled from a running simulator, see --live-url; "
			"data_path is then the folder that records the session)"
	)
	# 选择数据集类（对应 main 中实例化数据集的分支）。

	parser.add_argument(
		"--dataset-name",
		type=str,
		default=None,
		help="Dataset name for output directory structure (e.g., 'clio', 'habitat')"
	)
	# 仅用于输出目录的命名（如 clio / habitat），方便区分不同数据集的运行结果。

	# Model configuration — defaults are None so pipeline_config.yaml is the source of truth.
	# CLI args only override config when explicitly passed.
	# --- 模型配置（默认 None = 以 YAML 配置为准，CLI 只在显式传入时覆盖）---
	parser.add_argument(
		"--agent-model-name",
		type=str,
		default=None,
		help="Agent model name for grounding"
	)
	# 语义描述（grounding）用的智能体 / 多模态模型名称。

	parser.add_argument(
		"--sam-model",
		type=str,
		default=None,
		help="SAM model path"
	)
	# SAM 权重文件路径。

	parser.add_argument(
		"--sam-model-config-path",
		type=str,
		default=None,
		help="SAM model config path"
	)
	# SAM 模型结构配置路径（与权重文件配套使用）。

	parser.add_argument(
		"--sentence-embedding-model",
		type=str,
		default=None,
		help="Sentence embedding model"
	)
	# 文本嵌入模型：把语义标签 / 描述编码成向量，
	# 用于标签与文本描述之间的相似度计算（语义对齐）。

	parser.add_argument(
		"--depth-scale",
		type=float,
		default=None,
		help="Scale factor to convert depth to meters"
	)
	# 把原始深度值换算成米的缩放系数（不同数据集存储深度时单位不同）。

	parser.add_argument(
		"--fps",
		type=float,
		default=None,
		help="Dataset framerate"
	)
	# 数据集的录制帧率（用于时间对齐与实时模拟）。

	# LiveDataset options
	parser.add_argument(
		"--live-url", type=str, default="tcp://127.0.0.1:8002",
		help="LiveDataset: where the simulator bridge serves frames (scripts/habitat_live/bridge.py)"
	)
	parser.add_argument(
		"--no-live-record", action="store_true",
		help="LiveDataset: do not record the session into data_path"
	)
	parser.add_argument(
		"--live-connect-timeout", type=float, default=120.0,
		help="LiveDataset: seconds to wait for the simulator to come up"
	)

	# CODA-specific options
	# --- CODA 自动驾驶数据集专用选项 ---
	parser.add_argument(
		"--sequence", type=str, default=None,
		help="CODA sequence ID (e.g., '0', '4')"
	)
	# CODA 序列 ID。

	parser.add_argument(
		"--camera-id", type=str, default=None,
		help="CODA camera ID (e.g., 'cam0', 'cam1')"
	)
	# CODA 相机 ID。

	parser.add_argument(
		"--depth-source", type=str, default=None,
		help="CODA depth source ('3d_raw_estimated', '3d_raw', 'none')"
	)
	# CODA 深度来源：3d_raw_estimated（估计深度）/ 3d_raw（原始深度）/ none（无深度）。

	# Processing parameters
	# --- 处理参数 ---
	parser.add_argument(
		"--query-interval-frames",
		type=int,
		default=None,
		help="Query interval for grounding"
	)
	# 语义描述（grounding）的查询间隔：每 N 帧查询一次，而非每帧都查询
	# （多模态模型推理昂贵，间隔查询是保证实时性的关键）。

	parser.add_argument(
		"--num-assignment-workers",
		type=int,
		default=None,
		help="Number of assignment workers"
	)
	# 帧分配 worker 的进程数（并行度）。

	parser.add_argument(
		"--num-grounding-workers",
		type=int,
		default=None,
		help="Number of grounding workers"
	)
	# 语义描述 worker 的进程数（并行度）。

	parser.add_argument(
		"--assignment-worker",
		type=str,
		default=None,
		help="Assignment worker type"
	)
	# 选择帧分配策略实现（如 min_frames / min_frames_max_size）。

	parser.add_argument(
		"--grounding-worker",
		type=str,
		default=None,
		help="Grounding worker type"
	)
	# 选择语义描述后端实现（如 dam_multi_image）。

	parser.add_argument(
		"--min-mask-region-area",
		type=int,
		default=None,
		help="Minimum mask region area"
	)
	# 过滤过小的分割区域（单位：像素），避免噪声小区域进入下游处理。

	# Depth filtering
	# --- 深度范围过滤 ---
	parser.add_argument(
		"--depth-lb",
		type=float,
		default=None,
		help="Depth lower bound in meters"
	)
	# 深度下限（米）：过滤比它更近的点（通常是无效的近距离伪影）。

	parser.add_argument(
		"--depth-ub",
		type=float,
		default=None,
		help="Depth upper bound in meters"
	)
	# 深度上限（米）：过滤比它更远的点（过远的深度估计不可靠）。

	parser.add_argument(
		"--max-frames",
		type=int,
		default=None,
		help="Maximum number of frames to process"
	)
	# 只处理前 N 帧，便于快速调试（None = 不限制）。

	# Processing options
	# --- 运行行为选项 ---
	parser.add_argument(
		"--target-fps",
		type=float,
		default=None,
		help="Target framerate for processing (default: no throttling)"
	)
	# 限帧率：模拟在线系统的工作节奏，避免离线全速跑导致 worker 队列积压，
	# 也更贴近真实部署时的性能表现。

	parser.add_argument(
		"--no-wait-workers",
		action="store_true",
		help="Don't wait for workers to initialize"
	)
	# 不等所有 worker 初始化完成就开跑（启动更快，但可能丢前几帧）。

	parser.add_argument(
		"--no-throttle",
		action="store_true",
		help="Disable framerate throttling (overrides --target-fps)"
	)
	# 完全关闭限帧（优先级高于 --target-fps）。

	parser.add_argument(
		"--hydra-config-path",
		type=str,
		default="/path/to/daaam_ros/config/hydra_config/clio_dataset_khronos.yaml",
		help="Path to Hydra config YAML file"
	)
	# Hydra 后端自己的配置文件路径（后端里程计 / 建图参数）。
	# 注意：默认值只是一个占位路径，实际使用时必须改成真实路径。

	parser.add_argument(
		"--labelspace-path",
		type=str,
		default=None,
		help="Path to labelspace YAML file (optional)"
	)
	# Hydra 层的语义标签空间定义文件（与感知前端的语义配置对齐）。

	parser.add_argument(
		"--labelspace-colors",
		type=str,
		default=None,
		help="Labelspace colors CSV file"
	)
	# 标签配色 CSV（可视化时按标签着色）。

	parser.add_argument(
		"--zmq-url",
		type=str,
		default="tcp://127.0.0.1:8001",
		help="ZMQ URL for DSG publishing (use 'none' to disable)"
	)
	# 场景图发布地址：处理过程中通过 ZMQ 实时发布动态场景图（DSG），
	# 供外部可视化工具订阅；传 "none" 关闭。

	# Output options
	# --- 输出选项 ---
	parser.add_argument(
		"--output-dir",
		type=str,
		default=None,
		help="Output directory for results"
	)
	# 结果输出目录（覆盖配置中的 output_dir）。

	parser.add_argument(
		"--save-images",
		action="store_true",
		help="Save segmentation images"
	)
	# 是否按间隔保存分割可视化图片（见 create_frame_callback）。

	parser.add_argument(
		"--save-interval",
		type=int,
		default=100,
		help="Save images every N frames"
	)
	# 每 N 帧保存一张可视化图片。

	# Display options
	# --- 显示 / 调试选项 ---
	parser.add_argument(
		"--no-progress",
		action="store_true",
		help="Disable progress bar"
	)
	# 关闭进度条。

	parser.add_argument(
		"--no-logging",
		action="store_true",
		help="Disable logging"
	)
	# 关闭日志文件输出。

	parser.add_argument(
		"--verbose",
		action="store_true",
		help="Enable verbose output"
	)
	# 出错时打印完整异常堆栈（见 main 末尾的异常处理）。

	parser.add_argument(
		"--dry-run",
		action="store_true",
		help="Load dataset and config but don't run pipeline"
	)
	# 只加载配置和数据集就退出，不加载任何模型——调试配置问题的快速通道。

	# Semantic configuration
	# --- 语义配置 ---
	parser.add_argument(
		"--semantic-config",
		type=str,
		default=None,
		help="Semantic configuration file"
	)
	# 语义配置文件（标签定义等）。与 labelspace 存在回退关系（见 main 中取值链）。

	return parser.parse_args()


def apply_config_overrides(config: PipelineConfig, overrides: list[str]):
	"""Apply configuration overrides from command line.

	Args:
		config: Pipeline configuration
		overrides: List of key=value override strings
	"""
	# 把 "--config-overrides workers.num_grounding_workers=8" 这类
	# 「点号路径 key=value」字符串，转换成对嵌套配置对象的属性赋值。
	# 例：workers.num_grounding_workers=8 → config.workers.num_grounding_workers = 8
	if not overrides:
		return

	for override in overrides:
		if '=' not in override:
			print(f"Warning: Invalid override format: {override}")
			continue

		key, value = override.split('=', 1)
		keys = key.split('.')
		# 例："workers.num_grounding_workers" → ["workers", "num_grounding_workers"]

		# Navigate to the correct nested config
		# 逐层用 getattr 下钻，定位到「叶子字段的父对象」：
		# 例：config → config.workers，随后即可设置其 num_grounding_workers
		obj = config
		for k in keys[:-1]:
			if hasattr(obj, k):
				obj = getattr(obj, k)
			else:
				# 路径中途不存在则放弃整个 override，并给出出错路径的警告
				print(f"Warning: Unknown config key: {'.'.join(keys[:keys.index(k)+1])}")
				break
		else:
			# Python 的 for-else 语法：循环没有 break（即完整走完所有层级）才执行这里。
			# 用 for-else 保证「只有成功下钻到父对象时才赋值」。
			# Set the final value
			final_key = keys[-1]
			if hasattr(obj, final_key):
				# Try to parse the value type
				# 命令行传入的 value 永远是字符串，依据配置中现有值的类型做相应转换
				current_value = getattr(obj, final_key)
				if isinstance(current_value, bool):
					value = value.lower() in ['true', '1', 'yes']
				elif isinstance(current_value, int):
					value = int(value)
				elif isinstance(current_value, float):
					value = float(value)
				# else keep as string

				setattr(obj, final_key, value)
				print(f"Applied override: {key} = {value}")
			else:
				print(f"Warning: Unknown config key: {key}")


def create_frame_callback(save_images: bool, save_interval: int, output_dir: Path):
	"""Create a callback function for frame processing.

	Args:
		save_images: Whether to save images
		save_interval: Save every N frames
		output_dir: Output directory

	Returns:
		Callback function
	"""
	# 回调钩子：runner 每处理完一帧都会调用它（见 main 中 runner.run(frame_callback=...)），
	# 用于按固定间隔把分割可视化结果落盘，方便离线查看中间效果。
	if not save_images:
		return None

	images_dir = output_dir / "images"
	images_dir.mkdir(exist_ok=True)

	def callback(frame_id, frame, label_image, color_image, stats):
		"""Save images periodically."""
		# 只保存第 0、N、2N... 帧，避免输出目录爆炸
		if frame_id % save_interval == 0:
			import cv2

			# Save color segmentation
			# color_image 是 RGB 顺序的分割着色图；cv2.imwrite 需要 BGR，先转一次
			color_path = images_dir / f"segmentation_{frame_id:06d}.png"
			cv2.imwrite(str(color_path), cv2.cvtColor(color_image, cv2.COLOR_RGB2BGR))

			# Save label image
			# label_image 是逐像素的语义标签 ID 图；用 uint16 保存（标签数可能超过 255）
			label_path = images_dir / f"labels_{frame_id:06d}.png"
			cv2.imwrite(str(label_path), label_image.astype(np.uint16))

			print(f"Saved images for frame {frame_id}")

	return callback


def seed_clip_cache():
	"""Let BoxMOT's CLIP-ReID loader find OpenAI's ViT-B-16 weights locally instead of downloading them.

	The tracker builds its CLIP backbone through boxmot, which calls clip._download() and fetches ViT-B-16.pt
	(335 MiB) into ~/.cache/clip at every start; the ReID checkpoint is loaded on top of it afterwards. In a
	container ~/.cache is gone after each run, and without internet access the start-up would stall.
	scripts/download_models.py keeps the file in checkpoints/clip/; this links it into ~/.cache/clip, where
	clip._download() verifies its sha256 and skips the download.
	"""
	# boxmot 的 CLIP-ReID 每次启动都会联网下载 ViT-B-16.pt；把本地副本链接到它的缓存目录即可跳过下载
	src = Path(ROOT_DIR) / "checkpoints" / "clip" / "ViT-B-16.pt"
	dst = Path.home() / ".cache" / "clip" / "ViT-B-16.pt"
	if not src.is_file() or dst.is_file():
		return
	try:
		dst.parent.mkdir(parents=True, exist_ok=True)
		dst.unlink(missing_ok=True)  # a dangling link from an earlier run
		dst.symlink_to(src)
	except OSError as e:
		print(f"Warning: could not link {src} into {dst.parent} ({e}); boxmot will download the weights")


def main():
	"""Main entry point."""
	# ════════════════════════════════════════════════════════════════
	# 整体流程分 8 步（详见文件开头 docstring 中的流程图）。
	# ════════════════════════════════════════════════════════════════

	# ── 步骤 1：解析命令行参数 ──
	args = parse_args()
	seed_clip_cache()

	# ── 步骤 2：加载配置（PipelineConfig）──
	# Load or create configuration
	# 优先级：--config 指定的 YAML 文件 > 代码内默认值 PipelineConfig()
	if args.config:
		config_path = Path(args.config)
		if not config_path.is_absolute():
			# 相对路径基于仓库根目录 ROOT_DIR 解析，与脚本的运行目录无关
			config_path = Path(ROOT_DIR) / config_path
		print(f"Loading configuration from: {config_path}")
		config = PipelineConfig.from_yaml(str(config_path))
	else:
		print("Using default configuration")
		config = PipelineConfig()

	# ── 步骤 3：用命令行参数覆盖配置 ──
	# 设计约定：CLI 参数默认 None，仅在显式传入时才覆盖 YAML，
	# 使配置文件始终是唯一的事实来源（single source of truth）。
	# Apply CLI overrides only when explicitly passed (None = use config file value)
	# ---- 分割服务（segmentation）相关覆盖 ----
	if args.sam_model is not None:
		config.segmentation.model_name = args.sam_model
	if args.sam_model_config_path is not None:
		config.segmentation.model_config_path = args.sam_model_config_path
	if args.min_mask_region_area is not None:
		config.segmentation.min_mask_region_area = args.min_mask_region_area

	# ---- 语义描述服务（grounding）相关覆盖 ----
	if args.agent_model_name is not None:
		config.grounding.agent_model_name = args.agent_model_name
	if args.query_interval_frames is not None:
		config.grounding.query_interval_frames = args.query_interval_frames
	if args.sentence_embedding_model is not None:
		config.grounding.sentence_embedding_model = args.sentence_embedding_model

	# ---- worker 数量与类型覆盖 ----
	if args.num_assignment_workers is not None:
		config.workers.num_assignment_workers = args.num_assignment_workers
	if args.num_grounding_workers is not None:
		config.workers.num_grounding_workers = args.num_grounding_workers
	if args.assignment_worker is not None:
		config.workers.assignment_worker = args.assignment_worker
	if args.grounding_worker is not None:
		config.workers.grounding_worker = args.grounding_worker

	# ---- 深度范围过滤覆盖 ----
	if args.depth_lb is not None:
		config.depth.depth_lb = args.depth_lb
	if args.depth_ub is not None:
		config.depth.depth_ub = args.depth_ub

	# ---- 语义配置 / 标签配色覆盖 ----
	if args.semantic_config is not None:
		config.semantic_config_path = args.semantic_config
	if args.labelspace_colors is not None:
		config.labelspace_colors_path = args.labelspace_colors

	# ---- 数据集相关配置 ----
	# Dataset configuration
	config.dataset.data_path = args.data_path
	config.dataset.dataset_type = args.dataset_type
	if args.depth_scale is not None:
		config.dataset.depth_scale = args.depth_scale
	if args.fps is not None:
		config.dataset.fps = args.fps
	# 注意：max_frames 是无条件赋值的（None 也照常写入），None 表示不限制帧数。
	# 后续通过 runner.run(max_frames=...) 传入。
	config.dataset.max_frames = args.max_frames

	# ---- Hydra 后端配置（本脚本中恒启用）----
	# Apply Hydra configuration (always enabled)
	# 离线脚本强制启用 Hydra 场景图后端；ROS 版可以在运行时动态开关
	config.hydra.enable_hydra = True
	config.hydra.hydra_config_path = args.hydra_config_path
	# labelspace 的取值链：--labelspace-path 优先，否则回退到语义配置
	config.hydra.labelspace_path = args.labelspace_path or config.semantic_config_path
	config.hydra.labelspace_colors = config.labelspace_colors_path
	# ZMQ 用于把实时场景图发布给外部可视化工具；传 "none" 时关闭
	config.hydra.zmq_url = None if args.zmq_url.lower() == 'none' else args.zmq_url

	# Apply command-line overrides
	# 最后应用 --config-overrides：它的优先级最高，
	# 可以覆盖前面所有来源（包括 CLI 专用参数）设置过的任何字段
	apply_config_overrides(config, args.config_overrides)

	# 缓存里已有 DAM 权重就直接用本地快照目录：DAM 加载器对仓库名会调用 Hugging Face API，离线时会失败
	dam_config = config.workers.dam_grounding_config
	dam_config.dam_model_path = resolve_cached_hf_model(dam_config.dam_model_path)

	# Set output directory
	if args.output_dir:
		config.output_dir = args.output_dir

	# ── 步骤 4：实例化数据集 ──
	# Create dataset using the specified class
	print(f"Loading dataset from: {args.data_path}")
	print(f"Using dataset type: {args.dataset_type}")

	# 数据集通用配置：深度缩放（把原始深度值换算成米）、帧率，
	# 以及速度估计参数（为跟踪 / 建图提供帧间运动先验）。
	# 用 getattr 兜底：老版本配置没有这些字段时使用默认值。
	dataset_config = {
		"depth_scale": config.dataset.depth_scale,
		"fps": config.dataset.fps,
		"compute_velocities": getattr(config.dataset, 'compute_velocities', True),
		"velocity_window": getattr(config.dataset, 'velocity_window', 10),
		"velocity_alpha": getattr(config.dataset, 'velocity_alpha', 0.4)
	}

	try:
		# 三种数据集对应三种数据来源：
		#   CodaDataset         —— 自动驾驶数据集（CODA），按 sequence + camera_id 索引
		#   HM3DSemDataset      —— Habitat Matterport 3D 语义仿真数据集
		#   ImageSequenceDataset —— 通用图像序列文件夹（最常见用法）
		if args.dataset_type == "CodaDataset":
			dataset = CodaDataset(
				Path(args.data_path),
				config=dataset_config,
				sequence=args.sequence or "0",
				camera_id=args.camera_id or "cam0",
				depth_source=args.depth_source or "3d_raw_estimated",
				compute_velocities=dataset_config["compute_velocities"],
				velocity_window=dataset_config["velocity_window"],
				velocity_alpha=dataset_config["velocity_alpha"],
			)
		elif args.dataset_type == "HM3DSemDataset":
			dataset = HM3DSemDataset(
				Path(args.data_path),
				config=dataset_config,
				depth_scale=config.dataset.depth_scale,
				compute_velocities=dataset_config["compute_velocities"],
				velocity_window=dataset_config["velocity_window"],
				velocity_alpha=dataset_config["velocity_alpha"],
			)
		elif args.dataset_type == "LiveDataset":
			# frames come from a running simulator (scripts/habitat_live/bridge.py); data_path records the session
			dataset = LiveDataset(
				Path(args.data_path),
				config=dataset_config,
				url=args.live_url,
				fps=config.dataset.fps,
				record=not args.no_live_record,
				max_frames=config.dataset.max_frames,
				compute_velocities=dataset_config["compute_velocities"],
				velocity_window=dataset_config["velocity_window"],
				velocity_alpha=dataset_config["velocity_alpha"],
				connect_timeout=args.live_connect_timeout,
			)
		else:
			dataset = ImageSequenceDataset(
				Path(args.data_path),
				config=dataset_config,
				depth_scale=config.dataset.depth_scale,
				compute_velocities=dataset_config["compute_velocities"],
				velocity_window=dataset_config["velocity_window"],
				velocity_alpha=dataset_config["velocity_alpha"],
			)
		if isinstance(dataset, LiveDataset):
			print(f"Live source: {args.live_url} (frames are mapped as they arrive; the session is recorded in {args.data_path})" if not args.no_live_record
				else f"Live source: {args.live_url} (frames are mapped as they arrive; not recorded)")
		else:
			print(f"Loaded {len(dataset)} frames from dataset")
	except Exception as e:
		print(f"Error loading dataset: {e}")
		sys.exit(1)

	# Validate dataset
	# 检查路径存在性、RGB / 深度图数量是否匹配等，
	# 在加载模型之前就失败，避免运行到一半才发现数据有问题
	if not dataset.validate():
		print("Dataset validation failed")
		sys.exit(1)

	# Check for dry run
	# --dry-run：只验证「配置 + 数据集」能正确加载，不加载任何模型。
	# 省去几分钟的模型加载时间，是调试配置问题的快速通道
	if args.dry_run:
		print("\nDry run complete. Dataset and configuration loaded successfully.")
		print(f"Would process {len(dataset)} frames")
		print(f"Hydra config path: {config.hydra.hydra_config_path}")
		print(f"Labelspace path: {config.hydra.labelspace_path}")
		return

	# ── 步骤 5：创建 HydraPipelineRunner ──
	# Create Hydra pipeline runner (always used)
	output_dir = Path(config.output_dir) if config.output_dir else None

	print("Creating Hydra pipeline runner...")
	print(f"Hydra config path: {config.hydra.hydra_config_path}")
	print(f"Labelspace path: {config.hydra.labelspace_path}")

	# Handle target FPS from args
	# --target-fps 用于限帧率，模拟在线系统的工作节奏；
	# --no-throttle 优先级更高，完全关闭限帧
	target_fps = args.target_fps if not args.no_throttle else None

	# HydraPipelineRunner 在内部会：
	#   1) 创建 PipelineOrchestrator —— 感知前端（分割 / 跟踪 / 分配 / 语义描述）的总协调器
	#   2) 初始化 Hydra 后端 —— 3D 动态场景图构建（依赖深度 / 里程计）
	#   3) 启动所有 worker 子进程
	runner = HydraPipelineRunner(
		config=config,                                   # 感知前端配置
		dataset=dataset,                                 # 数据源
		hydra_config_path=config.hydra.hydra_config_path,  # Hydra 后端自己的配置（里程计 / 建图参数）
		labelspace_path=config.hydra.labelspace_path,      # 语义标签空间定义（与 Hydra 层对齐）
		labelspace_colors=config.hydra.labelspace_colors,  # 标签配色表（可视化用）
		output_dir=output_dir,                           # 结果输出目录（场景图、统计、图片）
		enable_logging=not args.no_logging,              # 是否写运行日志
		show_progress=not args.no_progress,              # 是否显示进度条
		target_fps=target_fps,                           # 限帧率（None = 全速）
		wait_for_workers=not args.no_wait_workers,       # 是否等所有 worker 就绪后再开始送帧
		match_ros_log_dir=True,  # Always match ROS log structure for consistency
		                         # 让日志目录结构与 ROS 版保持一致，便于对比两种入口的结果
		zmq_url=config.hydra.zmq_url,                    # 场景图实时发布地址（None = 关闭）
		glog_level=getattr(config.hydra, 'glog_level', 0),  # Hydra 的 glog 日志级别
		verbosity=getattr(config.hydra, 'verbosity', 0),    # 管道自身日志的详细程度
		dataset_name=args.dataset_name                   # 输出目录中数据集子目录名（如 clio / habitat）
	)

	# ── 步骤 6：注册帧回调（可选的可视化保存）──
	# Create callbacks
	frame_callback = create_frame_callback(
		args.save_images,
		args.save_interval,
		runner.output_dir
	)

	# ── 步骤 7：运行管道 ──
	# Run pipeline
	print(f"Starting pipeline processing...")
	print(f"Output will be saved to: {runner.output_dir}")

	if isinstance(dataset, LiveDataset):
		import signal

		def stop_live_stream(signum, _frame):
			# the first Ctrl-C / SIGTERM ends the stream: the frame being mapped is finished, then the map is saved;
			# a second one interrupts for good
			print("\n[live] stopping: finishing the current frame and saving the map (signal again to abort)", flush=True)
			dataset.request_stop()
			signal.signal(signum, signal.default_int_handler if signum == signal.SIGINT else signal.SIG_DFL)

		signal.signal(signal.SIGINT, stop_live_stream)
		signal.signal(signal.SIGTERM, stop_live_stream)

	try:
		# 这里是真正干活的地方：逐帧读取数据集，送进
		# 分割 → 跟踪 → 分配 → 语义描述 → 场景图 的处理流水线
		try:
			stats = runner.run(
				max_frames=config.dataset.max_frames,
				frame_callback=frame_callback
			)
		finally:
			if isinstance(dataset, LiveDataset):
				dataset.close()  # finishes the recording of the session
		if isinstance(dataset, LiveDataset):
			stats["total_frames"] = stats["frames_processed"]  # a live stream has no length
			stats["live"] = dataset.stats()

		# ── 步骤 8：输出统计与结果 ──
		# Print summary statistics
		# 各项统计的含义：
		#   frames_processed / total_frames —— 实际处理帧数 / 数据集总帧数
		#   avg_processing_time —— 感知前端单帧平均耗时（秒）
		#   cv_avg_time —— 视觉部分耗时：分割 + 跟踪 + 语义描述
		#   hydra_avg_time —— Hydra 后端建图耗时
		#   tracks_created —— BotSort 创建的目标轨迹数
		#   corrections_applied —— 对场景图应用的语义修正数
		#   dsg_nodes / dsg_edges —— 最终动态场景图（DSG）的节点数 / 边数
		print("\n" + "="*50)
		print("Processing Complete!")
		print("="*50)
		print(f"Frames processed: {stats['frames_processed']}/{stats['total_frames']}")

		if 'avg_processing_time' in stats:
			print(f"Average processing time: {stats['avg_processing_time']:.3f}s")
			print(f"Total processing time: {stats['total_processing_time']:.1f}s")

		if 'cv_avg_time' in stats:
			print(f"CV average time: {stats['cv_avg_time']:.3f}s")

		if 'hydra_avg_time' in stats:
			print(f"Hydra average time: {stats['hydra_avg_time']:.3f}s")

		print(f"Tracks created: {stats.get('tracks_created', 0)}")
		print(f"Corrections applied: {stats.get('corrections_applied', 0)}")

		if 'live' in stats:
			live = stats['live']
			print(
				f"Live session: {live['mapped_fps']:.2f} frames/s mapped, frame age on arrival {live['frame_age_ms_mean']:.0f} ms "
				f"(p95 {live['frame_age_ms_p95']:.0f} ms), motion between mapped frames {live['step_m_mean']:.2f} m / "
				f"{live['turn_deg_mean']:.1f} deg (max {live['step_m_max']:.2f} m / {live['turn_deg_max']:.0f} deg)"
			)

		if 'dsg_nodes' in stats:
			print(f"DSG nodes: {stats['dsg_nodes']}")
			print(f"DSG edges: {stats['dsg_edges']}")

		print(f"\nResults saved to: {runner.output_dir}")

		# Save final statistics
		# 统计信息写入 JSON 前先做 numpy 类型转换：
		# json.dump 不认识 np.float32 / np.ndarray 等类型，
		# 标量转 float，数组转 list
		stats_file = runner.output_dir / "final_stats.json"
		with open(stats_file, 'w') as f:
			# Convert numpy values for JSON
			json_stats = {}
			for key, value in stats.items():
				if isinstance(value, (np.ndarray, np.float32, np.float64, np.int32, np.int64)):
					json_stats[key] = float(value) if np.isscalar(value) else value.tolist()
				else:
					json_stats[key] = value
			json.dump(json_stats, f, indent=2)

	except KeyboardInterrupt:
		# Ctrl+C：优雅关闭 runner（停止 worker 进程、保存中间结果）后退出
		print("\n\nProcessing interrupted by user")
		runner.shutdown()
		sys.exit(1)
	except Exception as e:
		print(f"\nError during processing: {e}")
		if args.verbose:
			import traceback
			traceback.print_exc()
		sys.exit(1)


if __name__ == "__main__":
	# 退出挂起的规避：worker 被终止后，multiprocessing.Queue 的 feeder 线程可能卡在往已无读端的管道写数据上，
	# Python 解释器退出时会无限等待它（进程变成空闲但永不返回）。此时所有结果（dsg.json、corrections.yaml、
	# final_stats.json 等）早已在 main() 内写盘，所以直接退出，不再等待这些线程，同时保留退出码。
	exit_code = 0
	try:
		main()
	except SystemExit as e:
		exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
	except BaseException:
		import traceback
		traceback.print_exc()
		exit_code = 1
	finally:
		sys.stdout.flush()
		sys.stderr.flush()
		os._exit(exit_code)
