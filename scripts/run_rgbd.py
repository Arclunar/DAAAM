"""Run DAAAM on a prepared RGB-D directory shared with GS; no ROS/bag/calibration dependency."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shlex
import struct
import subprocess
import uuid

REPO = Path(__file__).resolve().parents[1]


def output_root():
    """Result root: DAAAM_OUTPUT_DIR (absolute; JellyMap uses runs/daaam), which compose mounts at output/ in the container."""
    return REPO/(os.environ.get('DAAAM_OUTPUT_DIR') or 'output')


def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--caller-dir', type=Path, default=Path.cwd(), help=argparse.SUPPRESS)
    p.add_argument('dataset', type=Path)
    p.add_argument('frames', nargs='?', default='all', help='positive count or all')
    p.add_argument('--target-fps', type=float, default=3.)
    p.add_argument('--depth-lb', type=float, default=.3)
    p.add_argument('--depth-ub', type=float, default=8.)
    p.add_argument('--uq', action='store_true', help='UQ-DAAAM: multi-view captions, uncertainty, active refinement and fusion (uq.enabled=true)')
    p.add_argument('--mv-preset', default='', help='multi-view delayed refinement preset (multiview.preset): baseline_daaam, qr, '
        'immediate_gamma, ours_full or ours_full.<ablation>; see src/daaam/multiview/config.py')
    p.add_argument('--mv', action='append', default=[], metavar='KEY=VALUE', help='multiview setting after the preset, e.g. --mv kappa=1 (repeatable)')
    p.add_argument('--intermediates', action='store_true', help='also write per-frame tracks, label images and query/description events to out_*/intermediates/ (save_intermediates=true)')
    profiling = p.add_mutually_exclusive_group()
    profiling.add_argument('--pyinstrument', action='store_true', help='sample main-thread calls and save an interactive HTML report')
    profiling.add_argument('--viztracer', action='store_true', help='record Python calls in the run directory (requires profile-install)')
    p.add_argument('--dry-run', action='store_true', help='validate and print command without launching Docker')
    a = p.parse_args(argv)
    path = a.dataset.expanduser()
    a.dataset = (path if path.is_absolute() else a.caller_dir/path).resolve()
    if a.uq and (a.mv_preset or a.mv):
        p.error('--uq and multiview (--mv-preset/--mv) are alternative description modules')
    if any('=' not in item or ' ' in item for item in a.mv):
        p.error('--mv takes KEY=VALUE without spaces')
    if a.frames != 'all' and (not a.frames.isdigit() or int(a.frames) <= 0):
        p.error('frames must be positive or all')
    if not all(math.isfinite(v) for v in (a.target_fps, a.depth_lb, a.depth_ub)) or \
            a.target_fps <= 0 or not 0 <= a.depth_lb < a.depth_ub:
        p.error('invalid target-fps or depth bounds')
    return a


def metadata(dataset):
    """Check the common pinhole RGB-D + T_world_camera contract before launching GPU work."""
    dataset = Path(dataset)
    try:
        info = json.loads((dataset/'camera_info.json').read_text())
        width, height = info['width'], info['height']
        if any(not isinstance(v, int) or isinstance(v, bool) or v <= 0 for v in (width, height)):
            raise ValueError('width/height must be positive integers')
        for field in ('fps', 'depth_scale'):
            if not math.isfinite(float(info[field])) or float(info[field]) <= 0:
                raise ValueError(field+' must be finite and positive')
        K = info['intrinsics']
        if len(K) != 3 or any(len(row) != 3 for row in K) or \
                not all(math.isfinite(v) for row in K for v in row) or K[0][0] <= 0 or K[1][1] <= 0 or \
                any(abs(a-b) > 1e-8 for a, b in zip(K[2], [0, 0, 1])) or abs(K[0][1])+abs(K[1][0]) > 1e-8:
            raise ValueError('intrinsics must be a zero-skew pinhole 3x3 matrix')
        if any(not math.isfinite(v) or abs(v) > 1e-12 for v in info.get('distortion', [])):
            raise ValueError('distortion must be zero; rectify RGB and depth during data preparation')
        image_formats = {'rgb': {'.jpg', '.jpeg', '.png', '.bmp'},
            'depth': {'.png', '.npy', '.exr', '.tiff'}}
        for folder, formats in image_formats.items():
            for path in (dataset/folder).iterdir():
                if path.suffix.lower() in formats and (not path.is_file() or path.suffix != '.png'):
                    raise ValueError('common RGB-D contract allows only lowercase .png (PNG): '+str(path))
        rgb = sorted((dataset/'rgb').glob('*.png'))
        depth = sorted((dataset/'depth').glob('*.png'))
        if not rgb or [p.name for p in rgb] != [p.name for p in depth]:
            raise ValueError('RGB and depth PNG names/counts must correspond one-to-one')
        for paths, depth_image in ((rgb, False), (depth, True)):
            for path in paths:
                with path.open('rb') as stream:
                    header = stream.read(29)
                if len(header) != 29 or header[:8] != b'\x89PNG\r\n\x1a\n' or header[12:16] != b'IHDR':
                    raise ValueError('invalid PNG header: '+str(path))
                w, h, bits, color, _, _, _ = struct.unpack('>IIBBBBB', header[16:29])
                if (w, h) != (width, height) or (depth_image and (bits, color) != (16, 0)) or \
                        (not depth_image and (bits != 8 or color not in (2, 6))):
                    raise ValueError('image shape/type differs from common RGB8/depth16 format: '+str(path))
        pose_path = dataset/'pose/poses.txt'
        if not pose_path.is_file():
            raise ValueError('missing pose/poses.txt; camera poses are required')
        poses = [line.split() for line in pose_path.read_text().splitlines() if line.strip()]
        if len(poses) != len(rgb):
            raise ValueError('pose count differs from RGB/depth count')
        for i, row in enumerate(poses):
            values = [float(v) for v in row]
            if len(values) != 16 or not all(math.isfinite(v) for v in values):
                raise ValueError('each pose must contain 16 finite row-major values')
            if any(abs(a-b) > 1e-6 for a, b in zip(values[12:], [0, 0, 0, 1])):
                raise ValueError('pose must be a rigid T_world_camera: '+str(i))
            R = [values[j:j+3] for j in (0, 4, 8)]
            det = (R[0][0]*(R[1][1]*R[2][2]-R[1][2]*R[2][1])
                - R[0][1]*(R[1][0]*R[2][2]-R[1][2]*R[2][0])
                + R[0][2]*(R[1][0]*R[2][1]-R[1][1]*R[2][0]))
            if abs(det-1) > 1e-3 or any(abs(sum(R[k][a]*R[k][b] for k in range(3))-(a == b)) > 1e-3
                    for a in range(3) for b in range(3)):
                raise ValueError('pose must be a rigid T_world_camera: '+str(i))
        stamps = dataset/'timestamps.txt'
        if stamps.is_file():
            t = [float(v) for v in stamps.read_text().split()]
            if len(t) != len(rgb) or not all(math.isfinite(v) for v in t) or any(b <= a for a, b in zip(t, t[1:])):
                raise ValueError('timestamps must be finite, increasing and correspond one-to-one with frames')
        return info, len(rgb)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise SystemExit('invalid prepared RGB-D directory: '+str(exc)) from exc


def command(a):
    dataset = a.dataset.expanduser().resolve()
    info, count = metadata(dataset)
    if a.frames != 'all' and int(a.frames) > count:
        raise SystemExit('requested frame count exceeds available frames: '+str(count))
    label = re.sub(r'[^A-Za-z0-9_-]+', '_', dataset.name).strip('_')[:60] or 'dataset'
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    multiview = bool(getattr(a, 'mv_preset', '') or getattr(a, 'mv', []))
    kind = 'uq-daaam' if getattr(a, 'uq', False) else ('mv-'+re.sub(r'[^A-Za-z0-9_.-]+', '_', a.mv_preset or 'custom')) if multiview else 'rgbd'
    name = (getattr(a, 'run_prefix', None) or kind)+'_'+label+'_'+stamp+'_'+uuid.uuid4().hex[:8]  # the workspace launcher names algorithm and reconciliation
    cmd = ['docker', 'compose', 'run', '--rm', '-T', '--name', name,
        '-v', str(dataset)+':/rgbd_input:ro', '-e', 'DATASET=/rgbd_input',
        '-e', 'NAME='+name, '-e', 'SEQ='+label, '-e', 'FPS='+str(float(info['fps'])),
        '-e', 'DEPTH_SCALE='+str(float(info['depth_scale'])), '-e', 'REFERENCE_MESH=off',
        '-e', 'DEPTH_LB='+str(a.depth_lb), '-e', 'DEPTH_UB='+str(a.depth_ub),
        'daaam-ws', 'bash', 'scripts/run_replay.sh', a.frames, str(a.target_fps)]
    flags = ['uq.enabled=true' if getattr(a, 'uq', False) else '', 'save_intermediates=true' if getattr(a, 'intermediates', False) else '']
    if multiview:  # the preset first so explicit --mv values win; frames are stamped index/fps, which maps them to motion.json rows
        motion = dataset/'.daaam/motion/motion.json'
        flags += ['multiview.enabled=true', 'multiview.dataset_fps='+str(float(info['fps']))] + \
            (['multiview.motion_path=/rgbd_input/.daaam/motion/motion.json'] if motion.is_file() else []) + \
            (['multiview.preset='+a.mv_preset] if a.mv_preset else []) + ['multiview.'+item for item in a.mv]
    if any(flags) or os.environ.get('EXTRA_OVERRIDES'):
        overrides = ' '.join(filter(None, [os.environ.get('EXTRA_OVERRIDES', ''), *flags]))
        cmd[cmd.index('daaam-ws'):cmd.index('daaam-ws')] = ['-e', 'EXTRA_OVERRIDES='+overrides]
    if a.viztracer:
        cmd[cmd.index('daaam-ws'):cmd.index('daaam-ws')] = ['-e', 'VIZTRACER=1']
    if a.pyinstrument:
        cmd[cmd.index("daaam-ws"):cmd.index("daaam-ws")] = ["-e", "PYINSTRUMENT=1"]
    return cmd, name


def main(argv=None):
    a = arguments(argv)
    cmd, name = command(a)
    out = output_root()
    result = out/name
    print('DAAAM results: '+str(result), flush=True)
    print(shlex.join(cmd), flush=True)
    if a.dry_run:
        return 0
    result.parent.mkdir(parents=True, exist_ok=True)
    with (out/'.rgbd-run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('another prepared RGB-D mapping run is active')
        guard = subprocess.run(['just', '_coda-guard'], cwd=str(REPO))
        if guard.returncode:
            return guard.returncode
        result.mkdir(parents=True, exist_ok=False)
        launch = {'dataset': str(a.dataset.expanduser().resolve()), 'frames': a.frames,
            'command': cmd, 'status': 'running', 'timestamp_policy': 'DAAAM uses frame_index/fps; timestamps.txt retained in source'}
        manifest = result/'rgbd_launch.json'
        manifest.write_text(json.dumps(launch, indent=2)+'\n')
        rc = 1
        try:
            with (result/'rgbd_run.log').open('w') as log:
                process = subprocess.Popen(cmd, cwd=str(REPO), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1)
                try:
                    for line in process.stdout:
                        print(line, end='', flush=True)
                        log.write(line)
                        log.flush()
                    rc = process.wait()
                except KeyboardInterrupt:
                    subprocess.run(['docker', 'stop', '-t', '30', name], check=False)
                    process.wait()
                    rc = 130
        finally:
            launch.update(status='exited', exit_code=rc, completed_utc=datetime.now(timezone.utc).isoformat())
            manifest.write_text(json.dumps(launch, indent=2)+'\n')
        return rc


if __name__ == '__main__':
    raise SystemExit(main())
