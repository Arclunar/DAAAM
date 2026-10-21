#!/usr/bin/env bash
# Map a simulator session live (container side of `just habitat-live`): pulls the frames of the Habitat bridge
# (scripts/habitat_live/bridge.py, running on the WSL host) into the DAAAM pipeline, publishes the scene graph over ZMQ
# and opens Hydra's streaming visualizer (RViz2), so the mesh, places, objects and rooms grow while someone walks.
#
# The bridge must already be serving frames; scripts/habitat_live.sh starts both. Inside the daaam-ws container (host
# networking, so 127.0.0.1 is the host's):
#   docker compose run --rm -T daaam-ws bash scripts/run_habitat_live.sh [MAX_FRAMES]
# MAX_FRAMES: stop after this many mapped frames; the default `all` runs until the simulator window is closed (Esc)
# or Ctrl-C, then the map is finished and saved like after any other run (output/<NAME>/out_*/).
#
# Environment overrides:
#   FPS: the run clock, frame k is stamped k / FPS (default 3, about the speed at which this machine maps with the viewer
#     open); it sets how long the object layer lags (tracker.temporal_window of the Hydra config is in these seconds).
#   LIVE_URL: where the bridge serves frames, default tcp://127.0.0.1:8002.
#   NAME: output subdirectory (default habitat_live); the recorded session is output/<NAME>/session_<time>/, a folder in
#     the layout of ImageSequenceDataset (RECORD=0 does not record: about 0.5 MB per mapped frame).
#   HYDRA_CONFIG: default config/hydra_config/habitat_house_khronos.yaml (house scale, 10 cm voxels).
#   DEPTH_LB / DEPTH_UB: depth range in metres, default 0.3 / 5.
#   DAM=1: describe the objects with DAM like the TUM runs do (about 12 GB of GPU and 9 GB of RAM; descriptions appear in
#     the saved result, not in RViz). The default 0 maps without it: lighter, and the map updates faster.
#   LABELS: size of the pseudo label space, default 5000 (see scripts/run_tum_example.sh).
#   VIEWER: 1 (default) opens RViz2, 0 does not; KEEP_VIEWER: 1 (default) keeps it open after the run for looking at the
#     final map until Ctrl-C, 0 closes it. VISUALIZER_CONFIG / RVIZ_CONFIG: the layer spacing of the scene graph and the RViz
#     layout, default config/hydra_config/habitat_visualizer.yaml and habitat_live.rviz (an apartment, not a building: the
#     defaults of hydra_visualizer stack the layers 11 to 23 m above the mesh); a missing file means the package's default.
#   REPLAY=1: also record the scene graph as the run goes and finalize a replay bundle and replay.rrd (just replay-web).
#   ZMQ_URL: where the scene graph is published, default tcp://127.0.0.1:8001; EXTRA_OVERRIDES: more key=value pipeline
#     overrides, space separated.
set -eo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

MAX_FRAMES="${1:-all}"
FPS="${FPS:-3}"
LIVE_URL="${LIVE_URL:-tcp://127.0.0.1:8002}"
NAME="${NAME:-habitat_live}"
LABELS="${LABELS:-5000}"
HYDRA_CONFIG="${HYDRA_CONFIG:-config/hydra_config/habitat_house_khronos.yaml}"
DEPTH_LB="${DEPTH_LB:-0.3}"
DEPTH_UB="${DEPTH_UB:-5.0}"
DAM="${DAM:-0}"
RECORD="${RECORD:-1}"
VIEWER="${VIEWER:-1}"
KEEP_VIEWER="${KEEP_VIEWER:-1}"
REPLAY="${REPLAY:-0}"
REPLAY_EVERY="${REPLAY_EVERY:-5}"
ZMQ_URL="${ZMQ_URL:-tcp://127.0.0.1:8001}"
VISUALIZER_CONFIG="${VISUALIZER_CONFIG:-config/hydra_config/habitat_visualizer.yaml}"
RVIZ_CONFIG="${RVIZ_CONFIG:-config/hydra_config/habitat_live.rviz}"
EXTRA_OVERRIDES="${EXTRA_OVERRIDES:-}"

# no core dumps: on WSL2 every crash of a large process writes a multi-GB dump file to the Windows drive
ulimit -c 0

if [ "${VIEWER}" = 1 ] && { [ -z "${DISPLAY:-}" ] || [ ! -d /tmp/.X11-unix ]; }; then
  echo "No X display in the container (DISPLAY='${DISPLAY:-}'): the RViz window cannot open (VIEWER=0 maps without it). See README.md for the X11 setup." >&2
  exit 1
fi
if [ ! -f "${HYDRA_CONFIG}" ]; then
  echo "Hydra config ${HYDRA_CONFIG} not found" >&2
  exit 1
fi
# Hydra publishes the scene graph by binding this address: a second publisher (another DAAAM run, see docker ps) would end
# the start of the pipeline with "Address already in use" after the models are loaded, so look first
zmq_endpoint="${ZMQ_URL#tcp://}"
if [[ "${ZMQ_URL}" == tcp://*:* ]] && (exec 3<>"/dev/tcp/${zmq_endpoint%:*}/${zmq_endpoint##*:}") 2>/dev/null; then
  echo "${ZMQ_URL} is already served by another process (a DAAAM run in another container? docker ps): wait for it, or set ZMQ_URL to a free port." >&2
  exit 1
fi
max_frames_args=(--max-frames "${MAX_FRAMES}")
if [ "${MAX_FRAMES}" = all ]; then
  max_frames_args=()  # until the simulator closes
fi
record_args=()
if [ "${RECORD}" != 1 ]; then
  record_args=(--no-live-record)
fi
dam_overrides=""
if [ "${DAM}" != 1 ]; then
  dam_overrides="workers.num_grounding_workers=0"
fi

LABELS_BASE="config/labels_pseudo.n${LABELS}"
if [ ! -f "${LABELS_BASE}.yaml" ]; then
  # a read-only config directory (container started with another uid than the image's) falls back to /tmp
  [ -w config ] || LABELS_BASE="/tmp/labels_pseudo.n${LABELS}"
  if [ ! -f "${LABELS_BASE}.yaml" ]; then
    python -c "from daaam.utils.vision import create_label_files; create_label_files('${LABELS_BASE}', ${LABELS})"
  fi
fi

SESSION="output/${NAME}/session_$(date +%Y%m%d_%H%M%S)"
mkdir -p "output/${NAME}"

pids=()
cleanup() {
  for pid in "${pids[@]}"; do kill "${pid}" 2>/dev/null || true; done
}
trap cleanup EXIT

viewer=""
if [ "${VIEWER}" = 1 ]; then
  # the launch arguments want absolute paths
  viewer_args=()
  if [ -f "${VISUALIZER_CONFIG}" ]; then viewer_args+=("visualizer_config_path:=$(realpath "${VISUALIZER_CONFIG}")"); fi
  if [ -f "${RVIZ_CONFIG}" ]; then viewer_args+=("rviz_path:=$(realpath "${RVIZ_CONFIG}")"); fi
  ros2 launch hydra_visualizer streaming_visualizer.launch.yaml use_zmq:=true zmq_url:="${ZMQ_URL}" "${viewer_args[@]}" &
  viewer=$!
  pids+=("${viewer}")
  # the viewer subscribes to the publisher; give it a few seconds to start before the first graph goes out
  sleep 8
fi

recorder=""
RECORDING="output/${NAME}/.replay-recording"
if [ "${REPLAY}" = 1 ]; then
  rm -rf "${RECORDING}"
  mkdir -p "${RECORDING}"
  python scripts/replay.py record --zmq-url "${ZMQ_URL}" --out "${RECORDING}" --every "${REPLAY_EVERY}" &
  recorder=$!
  pids+=("${recorder}")
  sleep 2  # the recorder subscribes to the publisher that the pipeline creates
fi
run_marker="$(mktemp)"

rc=0
# shellcheck disable=SC2086  # EXTRA_OVERRIDES is a space separated list of key=value pairs
python scripts/run_pipeline.py "${SESSION}" \
  --config config/pipeline_config.yaml \
  --dataset-type LiveDataset --live-url "${LIVE_URL}" "${record_args[@]}" \
  --hydra-config-path "${HYDRA_CONFIG}" \
  --sam-model fastsam/FastSAM-x.pt \
  --semantic-config "${LABELS_BASE}.yaml" \
  --labelspace-colors "${LABELS_BASE}.csv" \
  --fps "${FPS}" --depth-lb "${DEPTH_LB}" --depth-ub "${DEPTH_UB}" \
  --zmq-url "${ZMQ_URL}" --dataset-name "${NAME}" \
  "${max_frames_args[@]}" --no-throttle --no-progress \
  --save-images --save-interval 30 --verbose \
  --config-overrides tracking.reid_weights=checkpoints/reid_weights/clip_general.pt ${dam_overrides} ${EXTRA_OVERRIDES} || rc=$?

latest="$(find "output/${NAME}" -mindepth 1 -maxdepth 1 -type d -name 'out_*' -newer "${run_marker}" -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n 1 | cut -d' ' -f2-)"
rm -f "${run_marker}"

if [ -n "${recorder}" ]; then
  kill -TERM "${recorder}" 2>/dev/null || true
  recorder_rc=0
  wait "${recorder}" || recorder_rc=$?  # writes the final snapshot on SIGTERM
  if [ -n "${latest}" ] && [ -d "${SESSION}/rgb" ]; then
    if python scripts/replay.py finalize "${RECORDING}" \
        --run "${latest}" --dataset-kind tum --sequence "${NAME}" \
        --title "${NAME} / $(basename "${latest}")" \
        --dataset "${SESSION}" --fps "${FPS}" --out "${latest}/replay" \
      && python scripts/replay.py rrd "${RECORDING}" --run "${latest}" --fps "${FPS}" --out "${latest}/replay.rrd"; then
      echo "Replay bundle: ${latest}/replay (open it with: just replay-web)"
      echo "Rerun recording: ${latest}/replay.rrd"
      [ "${recorder_rc}" -ne 0 ] || rm -rf "${RECORDING}"
    else
      echo "Failed to finalize the replay artifacts; raw snapshots kept at ${RECORDING}" >&2
      [ "${rc}" -ne 0 ] || rc=1
    fi
  else
    echo "REPLAY=1 needs the recorded session (RECORD=1) and a result; raw snapshots kept at ${RECORDING}" >&2
  fi
fi

if [ -n "${latest}" ]; then
  if [ "${rc}" -eq 0 ] && [ -f "${latest}/dsg.json" ] && [ -d "${SESSION}/rgb" ]; then
    # Revisit the final native place geometry with the saved frames of the session (as run_tum_example.sh does)
    python scripts/lift_native_places.py --run "${latest}" --dataset "${SESSION}" || rc=$?
  fi
  python scripts/summarize_run.py "${latest}" --max-objects 10 || true
  [ ! -d "${SESSION}" ] || echo "Recorded session: ${SESSION} (map it again offline: DATASET=/data... see README.md)"
fi

if [ -n "${viewer}" ] && [ "${KEEP_VIEWER}" = 1 ]; then
  echo "The run is over (exit code ${rc}). The viewer stays open for looking at the final map; press Ctrl-C to close it."
  wait "${viewer}" || true
fi
exit "${rc}"
