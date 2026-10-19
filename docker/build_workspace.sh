#!/usr/bin/env bash
# The container entrypoint has loaded ROS, the venv and the previous install space.
set -eo pipefail
cd "${DAAAM_WS:-/opt/daaam_ws}"
export CMAKE_BUILD_PARALLEL_LEVEL="${CMAKE_BUILD_PARALLEL_LEVEL:-2}"
export MAKEFLAGS="${MAKEFLAGS:--j2}"
args=(build --base-paths src --continue-on-error --parallel-workers "${COLCON_WORKERS:-2}")
if [ "$#" -gt 0 ]; then
  args+=(--packages-up-to "$@")
fi
colcon "${args[@]}"
echo 'Build complete. New containers load this install space automatically.'
echo 'After changing Python binding C++ sources, run: bash docker/setup_workspace.sh pip-ws (inside just run).'
