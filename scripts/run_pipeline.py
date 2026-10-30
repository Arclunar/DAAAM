#!/usr/bin/env python3
"""Main script to run the daaam pipeline without ROS.

This script provides a command-line interface to process datasets through
the DAAAM pipeline with Hydra integration for scene graph generation.

Usage:
	python run_pipeline.py /path/to/dataset --config config/pipeline_config.yaml
	python run_pipeline.py /path/to/dataset --hydra-config habitat --target-fps 10
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Optional
import json
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from daaam.config import PipelineConfig
from daaam.datasets import ImageSequenceDataset, HM3DSemDataset, CodaDataset, LiveDataset
from daaam.hydra.runner import HydraPipelineRunner
from daaam import ROOT_DIR
from daaam.utils.hf_cache import resolve_cached_hf_model


def parse_args():
	"""Parse command line arguments."""
	parser = argparse.ArgumentParser(
		description="Run daaam pipeline on datasets without ROS"
	)

	# Required arguments
	parser.add_argument(
		"data_path",
		type=str,
		help="Path to dataset (folder for image sequences, file for rosbags)"
	)

	# Configuration
	parser.add_argument(
		"--config",
		type=str,
		default=None,
		help="Path to pipeline configuration YAML file"
	)

	parser.add_argument(
		"--config-overrides",
		type=str,
		nargs='+',
		help="Configuration overrides in key=value format (e.g., workers.num_grounding_workers=8)"
	)

	# Dataset options
	parser.add_argument(
		"--dataset-type",
		type=str,
		choices=["ImageSequenceDataset", "HM3DSemDataset", "CodaDataset", "LiveDataset"],
		default="ImageSequenceDataset",
		help="Dataset class name to use (LiveDataset: frames pulled from a running simulator, see --live-url; "
			"data_path is then the folder that records the session)"
	)

	parser.add_argument(
		"--dataset-name",
		type=str,
		default=None,
		help="Dataset name for output directory structure (e.g., 'clio', 'habitat')"
	)

	# Model configuration — defaults are None so pipeline_config.yaml is the source of truth.
	# CLI args only override config when explicitly passed.
	parser.add_argument(
		"--agent-model-name",
		type=str,
		default=None,
		help="Agent model name for grounding"
	)

	parser.add_argument(
		"--sam-model",
		type=str,
		default=None,
		help="SAM model path"
	)

	parser.add_argument(
		"--sam-model-config-path",
		type=str,
		default=None,
		help="SAM model config path"
	)

	parser.add_argument(
		"--sentence-embedding-model",
		type=str,
		default=None,
		help="Sentence embedding model"
	)

	parser.add_argument(
		"--depth-scale",
		type=float,
		default=None,
		help="Scale factor to convert depth to meters"
	)

	parser.add_argument(
		"--fps",
		type=float,
		default=None,
		help="Dataset framerate"
	)

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
	parser.add_argument(
		"--sequence", type=str, default=None,
		help="CODA sequence ID (e.g., '0', '4')"
	)

	parser.add_argument(
		"--camera-id", type=str, default=None,
		help="CODA camera ID (e.g., 'cam0', 'cam1')"
	)

	parser.add_argument(
		"--depth-source", type=str, default=None,
		help="CODA depth source ('3d_raw_estimated', '3d_raw', 'none')"
	)

	# Processing parameters
	parser.add_argument(
		"--query-interval-frames",
		type=int,
		default=None,
		help="Query interval for grounding"
	)

	parser.add_argument(
		"--num-assignment-workers",
		type=int,
		default=None,
		help="Number of assignment workers"
	)

	parser.add_argument(
		"--num-grounding-workers",
		type=int,
		default=None,
		help="Number of grounding workers"
	)

	parser.add_argument(
		"--assignment-worker",
		type=str,
		default=None,
		help="Assignment worker type"
	)

	parser.add_argument(
		"--grounding-worker",
		type=str,
		default=None,
		help="Grounding worker type"
	)

	parser.add_argument(
		"--min-mask-region-area",
		type=int,
		default=None,
		help="Minimum mask region area"
	)

	# Depth filtering
	parser.add_argument(
		"--depth-lb",
		type=float,
		default=None,
		help="Depth lower bound in meters"
	)

	parser.add_argument(
		"--depth-ub",
		type=float,
		default=None,
		help="Depth upper bound in meters"
	)

	parser.add_argument(
		"--max-frames",
		type=int,
		default=None,
		help="Maximum number of frames to process"
	)

	# Processing options
	parser.add_argument(
		"--target-fps",
		type=float,
		default=None,
		help="Target framerate for processing (default: no throttling)"
	)

	parser.add_argument(
		"--no-wait-workers",
		action="store_true",
		help="Don't wait for workers to initialize"
	)

	parser.add_argument(
		"--no-throttle",
		action="store_true",
		help="Disable framerate throttling (overrides --target-fps)"
	)

	parser.add_argument(
		"--hydra-config-path",
		type=str,
		default="/path/to/daaam_ros/config/hydra_config/clio_dataset_khronos.yaml",
		help="Path to Hydra config YAML file"
	)

	parser.add_argument(
		"--labelspace-path",
		type=str,
		default=None,
		help="Path to labelspace YAML file (optional)"
	)

	parser.add_argument(
		"--labelspace-colors",
		type=str,
		default=None,
		help="Labelspace colors CSV file"
	)

	parser.add_argument(
		"--zmq-url",
		type=str,
		default="tcp://127.0.0.1:8001",
		help="ZMQ URL for DSG publishing (use 'none' to disable)"
	)

	# Output options
	parser.add_argument(
		"--output-dir",
		type=str,
		default=None,
		help="Output directory for results"
	)

	parser.add_argument(
		"--save-images",
		action="store_true",
		help="Save segmentation images"
	)

	parser.add_argument(
		"--save-interval",
		type=int,
		default=100,
		help="Save images every N frames"
	)

	# Display options
	parser.add_argument(
		"--no-progress",
		action="store_true",
		help="Disable progress bar"
	)

	parser.add_argument(
		"--no-logging",
		action="store_true",
		help="Disable logging"
	)

	parser.add_argument(
		"--verbose",
		action="store_true",
		help="Enable verbose output"
	)

	parser.add_argument(
		"--dry-run",
		action="store_true",
		help="Load dataset and config but don't run pipeline"
	)

	# Semantic configuration
	parser.add_argument(
		"--semantic-config",
		type=str,
		default=None,
		help="Semantic configuration file"
	)

	return parser.parse_args()


def apply_config_overrides(config: PipelineConfig, overrides: list[str]):
	"""Apply configuration overrides from command line.

	Args:
		config: Pipeline configuration
		overrides: List of key=value override strings
	"""
	if not overrides:
		return

	for override in overrides:
		if '=' not in override:
			print(f"Warning: Invalid override format: {override}")
			continue

		key, value = override.split('=', 1)
		keys = key.split('.')

		# Navigate to the correct nested config
		obj = config
		for k in keys[:-1]:
			if hasattr(obj, k):
				obj = getattr(obj, k)
			else:
				print(f"Warning: Unknown config key: {'.'.join(keys[:keys.index(k)+1])}")
				break
		else:
			# Set the final value
			final_key = keys[-1]
			if hasattr(obj, final_key):
				# Try to parse the value type
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
			elif keys[0] == 'multiview':  # a mistyped switch would silently run another experiment
				raise SystemExit(f"Unknown multiview key: {key}")
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
	if not save_images:
		return None

	images_dir = output_dir / "images"
	images_dir.mkdir(exist_ok=True)

	def callback(frame_id, frame, label_image, color_image, stats):
		"""Save images periodically."""
		if frame_id % save_interval == 0:
			import cv2

			# Save color segmentation
			color_path = images_dir / f"segmentation_{frame_id:06d}.png"
			cv2.imwrite(str(color_path), cv2.cvtColor(color_image, cv2.COLOR_RGB2BGR))

			# Save label image
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
	# ════════════════════════════════════════════════════════════════

	args = parse_args()
	seed_clip_cache()

	# Load or create configuration
	if args.config:
		config_path = Path(args.config)
		if not config_path.is_absolute():
			config_path = Path(ROOT_DIR) / config_path
		print(f"Loading configuration from: {config_path}")
		config = PipelineConfig.from_yaml(str(config_path))
	else:
		print("Using default configuration")
		config = PipelineConfig()

	# Apply CLI overrides only when explicitly passed (None = use config file value)
	if args.sam_model is not None:
		config.segmentation.model_name = args.sam_model
	if args.sam_model_config_path is not None:
		config.segmentation.model_config_path = args.sam_model_config_path
	if args.min_mask_region_area is not None:
		config.segmentation.min_mask_region_area = args.min_mask_region_area

	if args.agent_model_name is not None:
		config.grounding.agent_model_name = args.agent_model_name
	if args.query_interval_frames is not None:
		config.grounding.query_interval_frames = args.query_interval_frames
	if args.sentence_embedding_model is not None:
		config.grounding.sentence_embedding_model = args.sentence_embedding_model

	if args.num_assignment_workers is not None:
		config.workers.num_assignment_workers = args.num_assignment_workers
	if args.num_grounding_workers is not None:
		config.workers.num_grounding_workers = args.num_grounding_workers
	if args.assignment_worker is not None:
		config.workers.assignment_worker = args.assignment_worker
	if args.grounding_worker is not None:
		config.workers.grounding_worker = args.grounding_worker

	if args.depth_lb is not None:
		config.depth.depth_lb = args.depth_lb
	if args.depth_ub is not None:
		config.depth.depth_ub = args.depth_ub

	if args.semantic_config is not None:
		config.semantic_config_path = args.semantic_config
	if args.labelspace_colors is not None:
		config.labelspace_colors_path = args.labelspace_colors

	# Dataset configuration
	config.dataset.data_path = args.data_path
	config.dataset.dataset_type = args.dataset_type
	if args.depth_scale is not None:
		config.dataset.depth_scale = args.depth_scale
	if args.fps is not None:
		config.dataset.fps = args.fps
	config.dataset.max_frames = args.max_frames

	# Apply Hydra configuration (always enabled)
	config.hydra.enable_hydra = True
	config.hydra.hydra_config_path = args.hydra_config_path
	config.hydra.labelspace_path = args.labelspace_path or config.semantic_config_path
	config.hydra.labelspace_colors = config.labelspace_colors_path
	config.hydra.zmq_url = None if args.zmq_url.lower() == 'none' else args.zmq_url

	# Apply command-line overrides
	apply_config_overrides(config, args.config_overrides)

	dam_config = config.workers.dam_grounding_config
	dam_config.dam_model_path = resolve_cached_hf_model(dam_config.dam_model_path)

	# Set output directory
	if args.output_dir:
		config.output_dir = args.output_dir

	# Create dataset using the specified class
	print(f"Loading dataset from: {args.data_path}")
	print(f"Using dataset type: {args.dataset_type}")

	dataset_config = {
		"depth_scale": config.dataset.depth_scale,
		"fps": config.dataset.fps,
		"compute_velocities": getattr(config.dataset, 'compute_velocities', True),
		"velocity_window": getattr(config.dataset, 'velocity_window', 10),
		"velocity_alpha": getattr(config.dataset, 'velocity_alpha', 0.4)
	}

	try:
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
	if not dataset.validate():
		print("Dataset validation failed")
		sys.exit(1)

	# Check for dry run
	if args.dry_run:
		print("\nDry run complete. Dataset and configuration loaded successfully.")
		print(f"Would process {len(dataset)} frames")
		print(f"Hydra config path: {config.hydra.hydra_config_path}")
		print(f"Labelspace path: {config.hydra.labelspace_path}")
		return

	# Create Hydra pipeline runner (always used)
	output_dir = Path(config.output_dir) if config.output_dir else None

	print("Creating Hydra pipeline runner...")
	print(f"Hydra config path: {config.hydra.hydra_config_path}")
	print(f"Labelspace path: {config.hydra.labelspace_path}")

	# Handle target FPS from args
	target_fps = args.target_fps if not args.no_throttle else None

	runner = HydraPipelineRunner(
		config=config,
		dataset=dataset,
		hydra_config_path=config.hydra.hydra_config_path,
		labelspace_path=config.hydra.labelspace_path,
		labelspace_colors=config.hydra.labelspace_colors,
		output_dir=output_dir,
		enable_logging=not args.no_logging,
		show_progress=not args.no_progress,
		target_fps=target_fps,
		wait_for_workers=not args.no_wait_workers,
		match_ros_log_dir=True,  # Always match ROS log structure for consistency
		zmq_url=config.hydra.zmq_url,
		glog_level=getattr(config.hydra, 'glog_level', 0),
		verbosity=getattr(config.hydra, 'verbosity', 0),
		dataset_name=args.dataset_name
	)

	# Create callbacks
	frame_callback = create_frame_callback(
		args.save_images,
		args.save_interval,
		runner.output_dir
	)

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

		# Print summary statistics
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
	if os.environ.get("DAAAM_MEMORY_STATS") == "trace":
		import tracemalloc
		tracemalloc.start(1)
	# Avoid waiting forever on orphaned multiprocessing queue feeder threads after results are saved.
	if os.environ.get("DAAAM_VIZTRACER_OUTPUT") and os.environ.get("DAAAM_PYINSTRUMENT_OUTPUT"):
		raise SystemExit("Select only one profiler per run")
	profiler = None
	if os.environ.get("DAAAM_PYINSTRUMENT_OUTPUT"):
		from pyinstrument import Profiler
		profiler = Profiler(interval=0.001)
		profiler.start()
	tracer = None
	if os.environ.get("DAAAM_VIZTRACER_OUTPUT"):
		from viztracer import VizTracer
		tracer = VizTracer(
			output_file=os.environ["DAAAM_VIZTRACER_OUTPUT"],
			include_files=[str(Path(__file__).resolve()), str(Path(__file__).resolve().parents[1] / "src" / "daaam")],
			ignore_c_function=True,
		)
		tracer.start()
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
		# os._exit bypasses atexit: persist the parent-process trace explicitly.
		if tracer is not None:
			try:
				tracer.stop()
				tracer.save()
			except Exception:
				import traceback
				traceback.print_exc()
				exit_code = exit_code or 1
		if profiler is not None:
			try:
				profiler.stop()
				from pyinstrument.renderers import JSONRenderer
				report = Path(os.environ["DAAAM_PYINSTRUMENT_OUTPUT"])
				report.parent.mkdir(parents=True, exist_ok=True)
				report.write_text(profiler.output_html(), encoding="utf-8")
				report.with_suffix(".txt").write_text(profiler.output_text(unicode=True, color=False), encoding="utf-8")
				report.with_suffix(".json").write_text(profiler.output(JSONRenderer()), encoding="utf-8")
			except Exception:
				import traceback
				traceback.print_exc()
				exit_code = exit_code or 1
		sys.stdout.flush()
		sys.stderr.flush()
		os._exit(exit_code)
