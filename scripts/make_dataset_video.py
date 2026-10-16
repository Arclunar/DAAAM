#!/usr/bin/env python3
"""Make a preview video of an image-sequence dataset (the rgb/ and depth/ folders written by scripts/prepare_tum_rgbd.py).

	python3 scripts/make_dataset_video.py datasets/tum_fr1_desk                  # -> output/dataset_preview/tum_fr1_desk.mp4
	python3 scripts/make_dataset_video.py datasets/tum_fr1_desk --no-depth       # the RGB frames only

Left: the RGB frame. Right: the depth image in colour (blue = near, red = far, black = no measurement), unless --no-depth.
Every frame carries its number and time. The video is H.264, so every player and browser plays it. Needs ffmpeg, OpenCV and
numpy; it runs in WSL as well as in the container.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np


def main() -> int:
	parser = argparse.ArgumentParser(description="Make a preview video of an image-sequence dataset")
	parser.add_argument("dataset", type=Path, help="dataset directory with rgb/, depth/, camera_info.json and timestamps.txt")
	parser.add_argument("--out", type=Path, default=None, help="output file (default: output/dataset_preview/<dataset name>.mp4)")
	parser.add_argument("--fps", type=float, default=None, help="frame rate of the video (default: the one in camera_info.json)")
	parser.add_argument("--no-depth", action="store_true", help="RGB only")
	parser.add_argument("--max-depth", type=float, default=4.0, help="depth in metres that maps to the far end of the colour scale")
	args = parser.parse_args()

	if shutil.which("ffmpeg") is None:
		print("ffmpeg not found (on Ubuntu: sudo apt install ffmpeg)", file=sys.stderr)
		return 1
	root = args.dataset
	info = json.loads((root / "camera_info.json").read_text()) if (root / "camera_info.json").exists() else {}
	fps = args.fps or float(info.get("fps", 15.0))
	depth_scale = float(info.get("depth_scale", 1000.0))
	files = sorted((root / "rgb").glob("*.png")) + sorted((root / "rgb").glob("*.jpg"))
	if not files:
		print(f"no images in {root / 'rgb'}", file=sys.stderr)
		return 1
	with_depth = not args.no_depth and (root / "depth").is_dir()

	stamps = None
	if (root / "timestamps.txt").exists():
		values = [float(line.split()[0]) for line in (root / "timestamps.txt").read_text().splitlines() if line.strip()]
		if len(values) == len(files):
			stamps = [v - values[0] for v in values]

	out = args.out or Path("output/dataset_preview") / f"{root.resolve().name}.mp4"
	out.parent.mkdir(parents=True, exist_ok=True)
	height, width = cv2.imread(str(files[0])).shape[:2]
	size = f"{width * (2 if with_depth else 1)}x{height}"
	ffmpeg = subprocess.Popen(
		["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", size, "-r", str(fps), "-i", "-",
		 "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-movflags", "+faststart", str(out)],
		stdin=subprocess.PIPE)

	for i, file in enumerate(files):
		frame = cv2.imread(str(file))
		if with_depth:
			depth = cv2.imread(str(root / "depth" / f"{file.stem}.png"), cv2.IMREAD_UNCHANGED)
			if depth is None:
				depth = np.zeros(frame.shape[:2], np.uint16)
			scaled = np.clip(depth.astype(np.float32) / depth_scale / args.max_depth * 255.0, 0, 255).astype(np.uint8)
			colour = cv2.applyColorMap(scaled, cv2.COLORMAP_JET)
			colour[depth == 0] = 0
			frame = np.hstack([frame, colour])
		text = f"{i + 1}/{len(files)}  t = {(stamps[i] if stamps else i / fps):.2f} s"
		cv2.putText(frame, text, (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4, cv2.LINE_AA)
		cv2.putText(frame, text, (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 1, cv2.LINE_AA)
		ffmpeg.stdin.write(frame.tobytes())
	ffmpeg.stdin.close()
	code = ffmpeg.wait()
	if code == 0:
		print(f"{len(files)} frames, {len(files) / fps:.1f} s at {fps:g} fps -> {out} ({out.stat().st_size / 1e6:.1f} MB)")
		distro = os.environ.get("WSL_DISTRO_NAME")
		if distro:
			print("In Windows: \\\\wsl.localhost\\" + distro + str(out.resolve()).replace("/", "\\"))
	return code


if __name__ == "__main__":
	sys.exit(main())
