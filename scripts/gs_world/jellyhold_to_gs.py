"""Jellyhold ROS1 topics to the existing posed RGB-D training format; no ROS installation required."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from . import rosbag_to_rgbd

WORKSPACE = Path(__file__).resolve().parents[2]
DEFAULT_CALIB = WORKSPACE / "raw/jellyhold/calib_results/jellyhold_cali"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--caller-dir", type=Path, default=Path.cwd(), help=argparse.SUPPRESS)
    parser.add_argument("bag", type=Path, help="ROS1 .bag file, or a session directory containing exactly one .bag")
    parser.add_argument("out", type=Path, nargs="?", help="default: datasets_ws/processed/jellyhold/<bag stem>")
    parser.add_argument("--calib", type=Path, default=DEFAULT_CALIB, help="directory containing camchain.yaml and chain.yaml")
    parser.add_argument("--pose-topic", default="/lidar_slam/odom", help="FAST-LIO poses of Livox IMU; imu_propagate can be selected explicitly")
    parser.add_argument("--time-offset", type=float, default=0.0, help="seconds added to RGB header time for pose lookup; calibration time shifts are not guessed")
    parser.add_argument("--max-frames", type=int, default=1000)
    parser.add_argument("--min-translation", type=float, default=0.05, help="keyframe spacing in metres")
    parser.add_argument("--min-rotation", type=float, default=5.0, help="keyframe spacing in degrees")
    parser.add_argument("--start", type=float, default=0.0, help="seconds after first RGB frame")
    parser.add_argument("--duration", type=float, default=0.0, help="0: through end of recording")
    parser.add_argument("--list", action="store_true", help="list bag topics without converting")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    caller = args.caller_dir.resolve()
    bag = (caller / args.bag.expanduser()).resolve()
    if bag.is_dir():
        bags = sorted(bag.glob("*.bag"))
        if len(bags) != 1:
            raise SystemExit(f"{bag}: expected exactly one .bag; pass an explicit file (found {len(bags)})")
        bag = bags[0]
    if not bag.is_file():
        raise SystemExit(f"Bag not found: {bag}")
    if args.list:
        return rosbag_to_rgbd.main([str(bag), "--list"])
    calib = (caller / args.calib.expanduser()).resolve()
    for name in ("camchain.yaml", "chain.yaml"):
        if not (calib / name).is_file():
            raise SystemExit(f"Missing calibration: {calib / name}")
    if args.max_frames < 1 or args.min_translation < 0 or args.min_rotation < 0 or args.start < 0 or args.duration < 0:
        parser.error("frame count must be positive; spacing and time window must be nonnegative")
    out = (caller / args.out.expanduser()).resolve() if args.out else WORKSPACE / "processed/jellyhold" / bag.stem
    command = [str(bag), str(out), "--rgb-topic", "/camera/color/image_raw/compressed",
        "--depth-topic", "/camera/aligned_depth_to_color/image_raw", "--info-topic", "/camera/color/camera_info",
        "--pose-topic", args.pose_topic, "--extrinsic", str(calib / "chain.yaml") + ":T_cam_livox",
        "--camchain", str(calib / "camchain.yaml") + ":cam0"]
    for option in ("time_offset", "max_frames", "min_translation", "min_rotation", "start", "duration"):
        command += ["--" + option.replace("_", "-"), str(getattr(args, option))]
    if args.overwrite:
        command.append("--overwrite")
    status = rosbag_to_rgbd.main(command)
    if status:
        return status
    report_path = out / "conversion.json"
    report = json.loads(report_path.read_text())
    saved = out / "calibration"
    saved.mkdir(exist_ok=True)
    checksums = {}
    for name in ("camchain.yaml", "chain.yaml"):
        shutil.copy2(str(calib / name), str(saved / name))
        checksums[name] = hashlib.sha256((saved / name).read_bytes()).hexdigest()
    report["jellyhold"] = {
        "pose_frame_semantics": "Livox IMU (declared by recording owner; original child_frame_id retained above)",
        "camera_pose_formula": "T_world_rgb = T_world_livox_imu @ inverse(T_cam_livox)",
        "depth_source": "RealSense depth aligned to RGB; 16UC1 assumed 0.001 metres/unit",
        "time_offset_policy": "explicit --time-offset only; no inferred camera-to-IMU clock shift",
        "calibration_sha256": checksums,
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"GS training input: {out}")
    print(f"From jellymap_dev: just datasets gs-train {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
