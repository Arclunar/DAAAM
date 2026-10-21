#!/usr/bin/env bash
# Watch the scene graph being built: starts Hydra's streaming visualizer (RViz2) and runs the TUM example with the
# scene graph published over ZMQ, so objects, places, rooms and the mesh appear in RViz while the frames are processed.
#
# Inside the daaam-ws container, which already has host networking, DISPLAY and the X11 socket (on WSL2 with WSLg
# the RViz window opens on the Windows desktop):
#   docker compose run --rm -T daaam-ws bash scripts/run_live_preview.sh [MAX_FRAMES] [TARGET_FPS]
#   docker compose run --rm -T daaam-ws bash scripts/run_live_preview.sh 298 3
#
# The arguments and the environment overrides are those of scripts/run_tum_example.sh. Environment: ZMQ_URL (default
# tcp://127.0.0.1:8001), KEEP_VIEWER (default 1: after the run the viewer stays open for inspecting the final map
# until Ctrl-C; 0 closes it when the run is over).
set -eo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

ZMQ_URL="${ZMQ_URL:-tcp://127.0.0.1:8001}"
KEEP_VIEWER="${KEEP_VIEWER:-1}"

# no core dumps: on WSL2 every crash of a large process writes a multi-GB dump file to the Windows drive
ulimit -c 0

if [ -z "${DISPLAY:-}" ] || [ ! -d /tmp/.X11-unix ]; then
  echo "No X display in the container (DISPLAY='${DISPLAY:-}'): the RViz window cannot open. See README.md for the X11 setup." >&2
  exit 1
fi

ros2 launch hydra_visualizer streaming_visualizer.launch.yaml use_zmq:=true zmq_url:="${ZMQ_URL}" &
viewer=$!
trap 'kill "${viewer}" 2>/dev/null || true' EXIT

# the viewer subscribes to the publisher; give it a few seconds to start before the first graph goes out
sleep 8

rc=0
ZMQ_URL="${ZMQ_URL}" bash scripts/run_tum_example.sh "$@" || rc=$?

if [ "${KEEP_VIEWER}" = 1 ]; then
  echo "The run is over (exit code ${rc}). The viewer stays open; press Ctrl-C to close it."
  wait "${viewer}" || true
fi
exit "${rc}"
