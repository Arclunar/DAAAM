#!/usr/bin/env bash
# Map a gs_world RGB-D folder (a 3DGS render, or a converted D455 bag) with DAAAM, the way `just tum-run` maps a TUM
# sequence: scripts/run_replay.sh -> scripts/run_tum_example.sh, with the replay bundle and replay.rrd in
# output/<NAME>/out_<time>/.
#
# Inside the daaam-ws container (just gs-daaam DATASET [MAX_FRAMES] [NAME]):
#   bash scripts/gs_world/run_daaam.sh /data/gs_world/models/tum_fr1_room/<time>/renders/original [all] [NAME]
#
# NAME defaults to gs_<dataset>_<trajectory> for renders (gs_tum_fr1_room_original) and gs_<folder> otherwise. The
# dataset's frame rate comes from its camera_info.json. Other environment overrides are those of
# scripts/run_tum_example.sh; REFERENCE_MESH defaults to off here (the 1 cm TSDF reference mesh is for real sensor data).
set -eo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

DATASET="${1:?usage: run_daaam.sh DATASET [MAX_FRAMES] [NAME]}"
MAX_FRAMES="${2:-all}"
NAME="${3:-${NAME:-}}"
if [ ! -d "${DATASET}/rgb" ] || [ ! -f "${DATASET}/camera_info.json" ]; then
  echo "${DATASET} is not an RGB-D dataset folder (rgb/, depth/, pose/poses.txt, camera_info.json)" >&2
  exit 1
fi
if [ -z "${NAME}" ]; then
  NAME="$(python - "${DATASET}" <<'EOF'
import sys
from pathlib import PurePosixPath

parts = PurePosixPath(sys.argv[1].rstrip("/")).parts
if "renders" in parts and 2 <= parts.index("renders") < len(parts) - 1:
	i = parts.index("renders")
	print(f"gs_{parts[i - 2]}_{parts[i + 1]}")
else:
	print(f"gs_{parts[-1]}")
EOF
)"
fi
FPS="$(python -c 'import json, sys; print(json.load(open(sys.argv[1])).get("fps") or 15)' "${DATASET}/camera_info.json")"
# the replay bundle shows SEQ as the dataset's name (a TUM-layout bundle; run_replay.sh would say fr1_desk)
SEQ="${SEQ:-${NAME}}"
echo "DAAAM on ${DATASET}: NAME=${NAME}, FPS=${FPS}, frames ${MAX_FRAMES}"
DATASET="${DATASET}" NAME="${NAME}" SEQ="${SEQ}" FPS="${FPS}" REPLAY_EVERY="${REPLAY_EVERY:-5}" \
  REFERENCE_MESH="${REFERENCE_MESH:-off}" bash scripts/run_replay.sh "${MAX_FRAMES}"
