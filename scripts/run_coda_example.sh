#!/usr/bin/env bash
# Run DAAAM + Hydra on a CODa rosbag the way RUNNING.md section 4 does (launch the pipeline, play the bag, wait for
# the end), with the settings validated on an RTX 5060 Ti (16 GB) / 15 GB RAM.
#
# Inside the daaam-ws container:
#   docker compose run --rm -T daaam-ws bash scripts/run_coda_example.sh [BAG]
#
# BAG is a bag directory (a path inside the container, or just its name under $CODA_ROOT_DIR); it defaults to the newest
# $CODA_ROOT_DIR/coda_0_with_depth*.bag (the bag writer appends a time stamp to the name; scripts/make_coda_bag.sh).
# Environment overrides: SCENE (default coda_sequence_0), LABELS (size of the pseudo label space, default 5000: Hydra
#   allocates about 1.1 GB + 0.28 MB per label, the default 30000 labels need ~9.5 GB), RATE (bag playback speed; the
#   bags are 10 Hz, so the default 0.3 plays 3 frames per second: a frame takes 130-150 ms on average here (p99 400-680 ms)
#   and the nodes drop what they cannot keep up with, which at 10 frames per second (100 ms each) would be many frames),
#   READY_TIMEOUT (seconds to wait for the models to load, default 600), OUT (where the launch/player logs go, default
#   $CODA_ROOT_DIR/../output/coda_run), EXTRA_LAUNCH_ARGS (more name:=value arguments for coda_daaam_hydra.launch.yaml,
#   e.g. "num_grounding_workers:=0" to leave DAM out).
#
#   CLIP_BACKEND, CLIP_MODEL (frame selection encoder; unset = the launch file's default, the Perception Encoder
#   PE-Core-L14-336; e.g. CLIP_BACKEND=openclip CLIP_MODEL=ViT-L-14 for the OpenCLIP model).
#
#   DAAAM_CLIP_HALF (default 1: the Perception Encoder in fp16), DAAAM_TRIM_INTERVAL_FRAMES (default 300: the DAAAM node
#   gives freed heap memory back to the OS every N frames, 0 = never), LONG_BAG_FRAMES (default 2600: a bag with more
#   frames gets a warning, see below).
#
# Notes
#   * The launch file's default engines (FastSAM-x-640x480.engine, clip_general.engine) are built with
#     scripts/export_fastsam_trt.py and scripts/export_vanilla_clip_engine.py (INSTALL.md). The Perception Encoder
#     checkpoint (facebook/PE-Core-L14-336, 2.7 GB) is downloaded by huggingface_hub into the Hugging Face cache;
#     here it sits in $DAAAM_DATA_DIR/hf_cache and is linked into the workspace cache so that no HF_HOME is needed.
#   * The results go to output/coda/ (a symlink into $CODA_ROOT_DIR/../output/coda when output/coda does not exist, so
#     that the grounding images do not grow the WSL disk on the system drive); Hydra's own log directory is
#     $OUT/hydra for the same reason.
#   * Replay bundle: the result written by this run (never an older one) gets RESULT/replay/, one snapshot of its final
#     dsg.json placed in time by the bag's daaam_replay.json (scripts/make_coda_bag.sh writes it; dataset kind, sequence,
#     first frame, frame count, frame rate and time origin are read from it, not from the bag's name). Open it with
#     `just replay-web`. A bag recorded before the sidecar existed keeps its mapping result but exits with 3 and says how
#     to record a new bag.
set -eo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

SCENE="${SCENE:-coda_sequence_0}"
LABELS="${LABELS:-5000}"
RATE="${RATE:-0.3}"
READY_TIMEOUT="${READY_TIMEOUT:-600}"
EXTRA_LAUNCH_ARGS="${EXTRA_LAUNCH_ARGS:-}"
OUT="${OUT:-${CODA_ROOT_DIR:?set CODA_ROOT_DIR}/../output/coda_run}"
BAG="${1:-$(ls -dt "${CODA_ROOT_DIR}"/coda_0_with_depth*.bag 2>/dev/null | head -n 1)}"
if [ ! -d "${BAG}" ] && [ -d "${CODA_ROOT_DIR}/${BAG}" ]; then BAG="${CODA_ROOT_DIR}/${BAG}"; fi
[ -d "${BAG}" ] || { echo "bag not found: '${BAG}' (record it with scripts/make_coda_bag.sh / just coda-bag)" >&2; exit 2; }

# A long bag does not fit into 15.9 GB RAM + 4 GB swap: the first run over the 8213 frames of sequence 0 died at frame
# 3599 (the kernel killed Hydra), chunks of about 2050 frames fit. Say so before spending an hour on it.
BAG_FRAMES="$(awk '/name: \/cam0\/rgb_image/ {found = 1} found && /message_count:/ {print $2; exit}' "${BAG}/metadata.yaml" 2>/dev/null || true)"
if [ -n "${BAG_FRAMES}" ] && [ "${BAG_FRAMES}" -gt "${LONG_BAG_FRAMES:-2600}" ]; then
  echo "WARNING: ${BAG} has ${BAG_FRAMES} frames: on 15.9 GB RAM + 4 GB swap a run this long is killed for lack of memory (USAGE.md 4.4); run it in chunks (just coda-chunks) or set LONG_BAG_FRAMES to silence this" >&2
fi

# no core dumps: on WSL2 every crash of a large process writes a multi-GB dump file to the Windows drive
ulimit -c 0

# The 16 GB GPU is full with DAM-3B and the engines: the Perception Encoder in fp32 (2.7 GB, both towers) pushed it over
# the limit (Windows then pages GPU memory: frames took 320 ms instead of 185 ms on average and 14% were dropped). In
# fp16 it takes 1.3 GB and gives the same features (cosine similarity 1.0000). DAAAM_CLIP_HALF=0 turns this off.
export DAAAM_CLIP_HALF="${DAAAM_CLIP_HALF:-1}"

# glibc keeps what Python/PyTorch/numpy freed inside the process. malloc_trim() returns those pages
# (daaam.utils.memory): about 0.5 GB in the grounding worker (once, after loading its models) and 70-80 MB per call in
# the DAAAM node (every DAAAM_TRIM_INTERVAL_FRAMES frames). It does not make a whole 8213-frame CODa sequence fit in
# 15.9 GB RAM + 4 GB swap: run such a sequence in chunks of about 2050 frames (USAGE.md 4.4).
export DAAAM_TRIM_INTERVAL_FRAMES="${DAAAM_TRIM_INTERVAL_FRAMES:-300}"

mkdir -p "${OUT}"
if [ ! -e output/coda ]; then
  mkdir -p output "${OUT}/../coda"
  ln -s "$(cd "${OUT}/../coda" && pwd)" output/coda
fi

LABELS_BASE="config/labels_pseudo.n${LABELS}"
if [ ! -f "${LABELS_BASE}.yaml" ]; then
  [ -w config ] || LABELS_BASE="/tmp/labels_pseudo.n${LABELS}"
  if [ ! -f "${LABELS_BASE}.yaml" ]; then
    python -c "from daaam.utils.vision import create_label_files; create_label_files('${LABELS_BASE}', ${LABELS})"
  fi
fi

# the bag publishes /tf_static with TRANSIENT_LOCAL durability, which a late subscriber misses without this override
printf '/tf_static: {depth: 1, durability: transient_local}\n' > /tmp/tf_overrides.yaml
export ROS_STATIC_TRANSFORM_BROADCASTER_QOS_OVERRIDES=/tmp/tf_overrides.yaml

clip_args=()
if [ -n "${CLIP_BACKEND:-}" ]; then
  clip_args=(selectframe_clip_backend:="${CLIP_BACKEND}" selectframe_clip_model_name:="${CLIP_MODEL:?set CLIP_MODEL together with CLIP_BACKEND}")
fi

finalize_replay() {   # the one-snapshot replay bundle of result $1; 3 = the result has no bundle
  local result="$1" sidecar="${BAG}/daaam_replay.json" fields kind seq start count fps origin
  if [ ! -f "${result}/dsg.json" ]; then
    echo "No replay bundle: ${result} has no dsg.json" >&2
    return 3
  fi
  if [ ! -f "${sidecar}" ]; then
    echo "No replay bundle for ${result}: ${BAG} has no daaam_replay.json (it was recorded before replay bundles; the" \
      "mapping result is kept). Record a new bag, which writes the sidecar: just coda-bag <start> <frames> <name>," \
      "for example: just coda-bag 0 1000" >&2
    return 3
  fi
  if ! fields="$(python3 - "${sidecar}" <<'EOF'
import json, re, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    assert data["dataset_kind"] == "coda", "dataset_kind must be coda"
    sequence = str(data["sequence"])
    assert re.fullmatch(r"[0-9A-Za-z_-]+", sequence), "bad sequence"
    start, count = data["start_frame"], data["frame_count"]
    assert type(start) is int and start >= 0 and type(count) is int and count >= 1, "bad frame range"
    fps, origin = float(data["fps"]), float(data["time_origin"])
    assert fps > 0 and origin > 0, "bad fps or time origin"
except Exception as error:
    sys.exit("%s: %s" % (sys.argv[1], error))
print("coda %s %d %d %r %r" % (sequence, start, count, fps, origin))
EOF
  )"; then
    echo "No replay bundle for ${result}: ${sidecar} is not a valid CODa daaam_replay.json" >&2
    return 3
  fi
  read -r kind seq start count fps origin <<< "${fields}"
  if ! python scripts/replay.py finalize --graph "${result}/dsg.json" --run "${result}" --dataset-kind "${kind}" --sequence "${seq}" \
      --dataset "${CODA_ROOT_DIR}" --fps "${fps}" --time-origin "${origin}" --start-frame "${start}" --frame-count "${count}" \
      --title "CODa ${seq} / $(basename "${result}")" --out "${result}/replay"; then
    echo "Failed to finalize the replay bundle of ${result} (the mapping result is kept)" >&2
    return 3
  fi
  echo "Replay bundle: ${result}/replay (open it with: just replay-web)"
}

run_marker="$(mktemp)"   # a result newer than this was written by this run
trap 'rm -f "${run_marker}"' EXIT

# shellcheck disable=SC2086
ros2 launch daaam_ros coda_daaam_hydra.launch.yaml scene:="${SCENE}" "${clip_args[@]}" log_path:="${OUT}/hydra" \
  labelspace_path:="$(pwd)/${LABELS_BASE}.yaml" labelspace_colors:="$(pwd)/${LABELS_BASE}.csv" \
  ${EXTRA_LAUNCH_ARGS} > "${OUT}/launch.log" 2>&1 &
launch=$!
trap 'kill -TERM "${launch}" 2>/dev/null || true; rm -f "${run_marker}"' EXIT

echo "Waiting for the pipeline to load its models (log: ${OUT}/launch.log) ..."
for ((i = 0; i < READY_TIMEOUT / 2; i++)); do
  grep -q "ROS components initialized" "${OUT}/launch.log" 2>/dev/null && break
  kill -0 "${launch}" 2>/dev/null || { echo "the launch exited early:" >&2; tail -n 20 "${OUT}/launch.log" >&2; exit 1; }
  sleep 2
done
grep -q "ROS components initialized" "${OUT}/launch.log" 2>/dev/null || { echo "the pipeline was not ready after ${READY_TIMEOUT} s" >&2; exit 1; }
sleep 5

echo "Playing ${BAG} at rate ${RATE} ..."
ros2 bag play "${BAG}" --clock -p -r "${RATE}" --qos-profile-overrides-path /tmp/tf_overrides.yaml > "${OUT}/play.log" 2>&1 &
play=$!
sleep 5
ros2 service call /rosbag2_player/resume rosbag2_interfaces/srv/Resume > /dev/null
wait "${play}" || true

echo "The bag has finished; waiting for Hydra and the DAAAM node to save their results ..."
for ((i = 0; i < 300; i++)); do
  kill -0 "${launch}" 2>/dev/null || break
  sleep 2
done
rc=0
wait "${launch}" || rc=$?
trap 'rm -f "${run_marker}"' EXIT
result="$(find output/coda/ -mindepth 1 -maxdepth 1 -type d -name 'out_*' -newer "${run_marker}" -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n 1 | cut -d' ' -f2-)"
artifact_rc=0
if [ -n "${result}" ]; then
  echo "Launch finished (exit code ${rc}). Results: ${result}"
  finalize_replay "${result}" || artifact_rc=$?
else
  echo "Launch finished (exit code ${rc}). No new result in output/coda: nothing to finalize" >&2
  artifact_rc=3
fi
[ "${rc}" -ne 0 ] || rc="${artifact_rc}"
exit "${rc}"
