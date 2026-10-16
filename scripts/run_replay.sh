#!/usr/bin/env bash
# Run the TUM example without RViz while the scene graph is recorded, then build two timed review artifacts: a
# self-contained WebGL page and a native Rerun recording (output/<NAME>/out_*/replay.{html,rrd}).
#
# Inside the daaam-ws container:
#   docker compose run --rm -T daaam-ws bash scripts/run_replay.sh [MAX_FRAMES] [TARGET_FPS]
#   docker compose run --rm -T daaam-ws bash scripts/run_replay.sh 298
#
# The page also shows the RGB and depth image of each moment (from DATASET, FPS: as in scripts/run_tum_example.sh).
# The arguments and the environment overrides are those of scripts/run_tum_example.sh. Environment: ZMQ_URL (default
# tcp://127.0.0.1:8001), REPLAY_EVERY (keep every n-th scene graph, default 5; about one is published per frame),
# REPLAY_KEEP=1 (keep the raw recording in output/<NAME>/.replay-recording, to rebuild the page with scripts/replay.py).
#
# 1 cm reference mesh: after the mapping run (so it costs the run no speed) the page gets a static Open3D TSDF mesh of the
# whole dataset (scripts/build_reference_mesh.py; cached in <DATASET>/.daaam/reference_mesh/, built once, reused by every later
# replay of the same dataset) that it can show instead of Hydra's mesh. REFERENCE_MESH=auto (default) reuses a valid cache or
# builds it, and still produces the dynamic-only page when that fails (Open3D missing, read-only dataset: see below);
# off skips it; rebuild replaces the cache, and a failure then makes the whole run fail. REFERENCE_MESH_CACHE=DIR keeps the
# cache files in DIR instead (a read-only dataset; one directory per dataset). The Rerun recording is not affected.
set -eo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

ZMQ_URL="${ZMQ_URL:-tcp://127.0.0.1:8001}"
REPLAY_EVERY="${REPLAY_EVERY:-5}"
SEQ="${SEQ:-fr1_desk}"  # the test set, see scripts/run_tum_example.sh
NAME="${NAME:-tum_${SEQ}}"
DATASET="${DATASET:-/datasets/tum_${SEQ}}"
FPS="${FPS:-15}"
REFERENCE_MESH="${REFERENCE_MESH:-auto}"
case "${REFERENCE_MESH}" in
  auto|off|rebuild) ;;
  *) echo "REFERENCE_MESH must be auto, off or rebuild (got '${REFERENCE_MESH}')" >&2; exit 2 ;;
esac

# no core dumps: on WSL2 every crash of a large process writes a multi-GB dump file to the Windows drive
ulimit -c 0

RECORDING="output/${NAME}/.replay-recording"
rm -rf "${RECORDING}"
mkdir -p "${RECORDING}"
python scripts/replay.py record --zmq-url "${ZMQ_URL}" --out "${RECORDING}" --every "${REPLAY_EVERY}" &
recorder=$!
run_marker="$(mktemp)"
trap 'kill "${recorder}" 2>/dev/null || true; rm -f "${run_marker}"' EXIT
sleep 2  # the recorder subscribes to the publisher that the pipeline creates

rc=0
ZMQ_URL="${ZMQ_URL}" bash scripts/run_tum_example.sh "$@" || rc=$?

kill -TERM "${recorder}" 2>/dev/null || true
recorder_rc=0
if wait "${recorder}"; then  # writes the final snapshot on SIGTERM
  :
else
  recorder_rc=$?
  echo "Replay recorder failed with exit code ${recorder_rc}; raw snapshots will be kept" >&2
  [ "${rc}" -ne 0 ] || rc="${recorder_rc}"
fi

run_dir="$(find "output/${NAME}" -mindepth 1 -maxdepth 1 -type d -name 'out_*' -newer "${run_marker}" -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n 1 | cut -d' ' -f2-)"
rm -f "${run_marker}"
dataset_args=()
if [ -d "${DATASET}/rgb" ]; then
  dataset_args=(--dataset "${DATASET}" --fps "${FPS}")  # the page then shows the RGB and depth image of each moment
fi
if [ -n "${run_dir}" ]; then
  reference_args=()
  if [ "${REFERENCE_MESH}" != off ] && [ -d "${DATASET}/rgb" ]; then
    build_args=("${DATASET}")
    [ -z "${REFERENCE_MESH_CACHE:-}" ] || build_args+=(--cache-dir "${REFERENCE_MESH_CACHE}")
    [ "${REFERENCE_MESH}" != rebuild ] || build_args+=(--force)
    # the builder prints only the path of the cache on stdout (progress and warnings go to stderr)
    if reference_path="$(python scripts/build_reference_mesh.py "${build_args[@]}")" && [ -n "${reference_path}" ]; then
      reference_args=(--reference-mesh "${reference_path}")
    elif [ "${REFERENCE_MESH}" = rebuild ]; then
      echo "REFERENCE_MESH=rebuild: the 1 cm reference mesh could not be built" >&2
      [ "${rc}" -ne 0 ] || rc=1
    else
      echo "1 cm reference mesh unavailable; building the dynamic-only replay (REFERENCE_MESH=off silences this)" >&2
    fi
  fi
  if python scripts/replay.py build "${RECORDING}" --run "${run_dir}" --title "${NAME} / $(basename "${run_dir}")" "${dataset_args[@]}" "${reference_args[@]}" --out "${run_dir}/replay.html" \
    && python scripts/replay.py rrd "${RECORDING}" --run "${run_dir}" --fps "${FPS}" --out "${run_dir}/replay.rrd"; then
    echo "Review page: ${run_dir}/replay.html"
    echo "Rerun recording: ${run_dir}/replay.rrd"
	  if [ "${rc}" -eq 0 ] && [ "${recorder_rc}" -eq 0 ]; then
	    [ "${REPLAY_KEEP:-0}" = 1 ] || rm -rf "${RECORDING}"
	  else
	    echo "Replay artifacts may be partial; raw snapshots kept at ${RECORDING}" >&2
	  fi
  else
    echo "Failed to build one or more replay artifacts; raw snapshots kept at ${RECORDING}" >&2
    [ "${rc}" -ne 0 ] || rc=1
  fi
else
  echo "No output run directory was created by this invocation; raw snapshots kept at ${RECORDING}" >&2
  [ "${rc}" -ne 0 ] || rc=1
fi
exit "${rc}"
