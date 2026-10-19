#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v docker >/dev/null; then
  echo "ERROR: docker is not installed" >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: current user cannot access the Docker daemon" >&2
  exit 1
fi
if ! docker info --format '{{json .Runtimes}}' | grep -q 'nvidia'; then
  echo "ERROR: NVIDIA Container Toolkit is not configured" >&2
  exit 1
fi

docker compose config --quiet
docker compose run --rm -T daaam-ws bash -c '
set -euo pipefail
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
test "$ROS_DISTRO" = jazzy
test "$VIRTUAL_ENV" = /opt/daaam_ws/.venv
test -w /opt/daaam_ws
test -f /opt/daaam_ws/src/daaam/pyproject.toml
ros2 --help >/dev/null
command -v colcon >/dev/null
python --version
cmake --version | head -n1
echo "DAAAM development environment is ready."
'
