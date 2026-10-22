#!/usr/bin/env bash
# Steer a Habitat agent with the keyboard and watch DAAAM build the scene graph live (WSL host side of `just habitat-live`).
#
# Starts the Habitat bridge here on the host (the habitat-sim conda environment of ~/jellymap_dev/habitat_ws, nothing is
# installed into it: a window with the agent's first-person view, W/S walk, A/D strafe, arrows or Q/E turn and look, Shift
# faster, R back to the start, P autopilot, Esc quit), waits until it serves frames, then runs the mapper in the daaam-ws
# container (scripts/run_habitat_live.sh: RViz2 with the growing scene graph, DAAAM, Hydra). When one side ends the other
# is stopped: closing the Habitat window (or Ctrl-C) finishes and saves the map; the container shares the host's network.
#
#   bash scripts/habitat_live.sh [MAX_FRAMES]            # until Esc / Ctrl-C (MAX_FRAMES: stop after that many mapped frames)
#   bash scripts/habitat_live.sh --bridge-only [ARGS]    # only the Habitat window and frame server (ARGS: scripts/habitat_live/bridge.py)
#
# Environment: HABITAT_PYTHON (default ~/miniforge3/envs/habitat-smallroom/bin/python), HABITAT_DATA (default
# ~/jellymap_dev/habitat_ws/data: replica_cad/ lives there), BRIDGE_PORT (8002), SCENE (apt_1), AUTOPILOT=1 (start with the
# autopilot walking to unseen places), RUN_SECONDS=N (close the stream N seconds after the first mapped frame), BRIDGE_ARGS
# (more arguments of bridge.py), and the variables of scripts/run_habitat_live.sh (FPS, NAME, DAM, HYDRA_CONFIG, VIEWER,
# KEEP_VIEWER, REPLAY, RECORD, ...), which are passed on to the container.
set -eo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

HABITAT_PYTHON="${HABITAT_PYTHON:-$HOME/miniforge3/envs/habitat-smallroom/bin/python}"
export HABITAT_DATA="${HABITAT_DATA:-$HOME/jellymap_dev/habitat_ws/data}"
BRIDGE_PORT="${BRIDGE_PORT:-8002}"
NAME="${NAME:-habitat_live}"
SCENE="${SCENE:-apt_1}"

bridge_only=0
if [ "${1:-}" = "--bridge-only" ]; then
  bridge_only=1
  shift
fi

if [ ! -x "${HABITAT_PYTHON}" ]; then
  echo "No Habitat environment at ${HABITAT_PYTHON} (set HABITAT_PYTHON to the python of a conda environment with habitat-sim, see README.md)." >&2
  exit 1
fi
if [ "${SCENE}" = apt_1 ] && [ ! -f "${HABITAT_DATA}/replica_cad/replicaCAD.scene_dataset_config.json" ]; then
  echo "No ReplicaCAD at ${HABITAT_DATA}/replica_cad (set HABITAT_DATA, or SCENE and BRIDGE_ARGS=--scene-dataset ... for another scene, see README.md)." >&2
  exit 1
fi
if [ -z "${DISPLAY:-}" ]; then
  echo "No DISPLAY: the Habitat window and RViz need WSLg (a desktop session on Windows 11, or X11)." >&2
  exit 1
fi
port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
if port_open "${BRIDGE_PORT}"; then
  echo "Port ${BRIDGE_PORT} is already in use (another bridge running? set BRIDGE_PORT)." >&2
  exit 1
fi

bridge_args=(--port "${BRIDGE_PORT}" --scene "${SCENE}")
[ "${AUTOPILOT:-0}" != 1 ] || bridge_args+=(--autopilot)
[ -z "${RUN_SECONDS:-}" ] || bridge_args+=(--run-seconds "${RUN_SECONDS}")
# shellcheck disable=SC2206  # BRIDGE_ARGS is a space separated list on purpose
[ -z "${BRIDGE_ARGS:-}" ] || bridge_args+=(${BRIDGE_ARGS})

# Habitat renders on the GPU through Mesa's D3D12 driver under WSLg (process-local; the default is software rendering)
export GALLIUM_DRIVER="${GALLIUM_DRIVER:-d3d12}"
export MESA_D3D12_DEFAULT_ADAPTER_NAME="${MESA_D3D12_DEFAULT_ADAPTER_NAME:-NVIDIA}"

if [ "${bridge_only}" = 1 ]; then
  exec "${HABITAT_PYTHON}" scripts/habitat_live/bridge.py "${bridge_args[@]}" "$@"
fi

mkdir -p "output/${NAME}"
stamp="$(date +%Y%m%d_%H%M%S)"
bridge_log="output/${NAME}/bridge_${stamp}.log"
container="daaam-habitat-live-$$"
bridge=""
follower=""
mapper=""
cleanup() {
  [ -z "${follower}" ] || kill "${follower}" 2>/dev/null || true
  if [ -n "${bridge}" ] && kill -0 "${bridge}" 2>/dev/null; then
    kill -TERM "${bridge}" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "${bridge}" 2>/dev/null || break; sleep 1; done
    kill -KILL "${bridge}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

"${HABITAT_PYTHON}" scripts/habitat_live/bridge.py "${bridge_args[@]}" > "${bridge_log}" 2>&1 &
bridge=$!
tail --pid="$$" -n +1 -F "${bridge_log}" 2>/dev/null | grep --line-buffered '^\[bridge\]' &
follower=$!

echo "Starting the Habitat bridge (log: ${bridge_log}) ..."
ready=0
for _ in $(seq 1 240); do
  if ! kill -0 "${bridge}" 2>/dev/null; then
    echo "The Habitat bridge exited before it served frames; its log:" >&2
    grep -v -E 'navmesh_instances|Glob path result' "${bridge_log}" | tail -n 25 >&2
    exit 1
  fi
  if port_open "${BRIDGE_PORT}"; then ready=1; break; fi
  sleep 0.5
done
if [ "${ready}" != 1 ]; then
  echo "The Habitat bridge did not come up within 120 s; see ${bridge_log}" >&2
  exit 1
fi

cat <<EOF

The Habitat window is open: click it first (RViz takes the focus when it opens), then W/S walk, A/D strafe, arrows or Q/E
turn and look, Shift faster, R back to the start, P autopilot, Esc quits. RViz opens in about 10 s and the map starts to grow
after the models are loaded (about 1 minute). Closing the Habitat window or Ctrl-C finishes and saves the map.

EOF

# the variables that the container script understands go along when they are set here
env_args=(-e "LIVE_URL=tcp://127.0.0.1:${BRIDGE_PORT}" -e "NAME=${NAME}")
for variable in FPS LABELS HYDRA_CONFIG DEPTH_LB DEPTH_UB DAM RECORD VIEWER KEEP_VIEWER REPLAY REPLAY_EVERY ZMQ_URL EXTRA_OVERRIDES; do
  if [ -n "${!variable+x}" ]; then env_args+=(-e "${variable}"); fi
done

stopping=0
on_signal() {
  if [ "${stopping}" = 0 ]; then
    stopping=1
    if kill -0 "${bridge}" 2>/dev/null; then
      echo "Stopping: closing the simulator, the mapper finishes and saves the map (Ctrl-C again stops it at once)."
      kill -TERM "${bridge}" 2>/dev/null || true
    else
      echo "The simulator is closed already: the mapper is saving the map, or RViz stays open for looking at it (close the RViz window to finish; Ctrl-C again stops everything at once)."
    fi
  else
    docker stop "${container}" > /dev/null 2>&1 || true
  fi
}
trap on_signal INT TERM

docker compose run --rm -T --name "${container}" "${env_args[@]}" daaam-ws bash scripts/run_habitat_live.sh "$@" &
mapper=$!
rc=0
while kill -0 "${mapper}" 2>/dev/null; do
  wait "${mapper}" 2>/dev/null && rc=0 || rc=$?
done
trap - INT TERM
echo "The mapper has finished (exit code ${rc})."
exit "${rc}"
