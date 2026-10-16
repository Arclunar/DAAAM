#!/usr/bin/env bash
set -e

source "/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"

# A fresh or externally supplied workspace volume may not contain the venv that
# was seeded into the image. Recreate only the empty environment when needed;
# installing DAAAM and its dependencies remains an explicit user action.
if [[ ! -x "${VIRTUAL_ENV}/bin/python" ]]; then
  python3 -m venv --system-site-packages "${VIRTUAL_ENV}"
fi
source "${VIRTUAL_ENV}/bin/activate"

if [[ -f /opt/daaam_ws/install/setup.bash ]]; then
  source /opt/daaam_ws/install/setup.bash
fi

exec "$@"
