#!/usr/bin/env bash
# Record a rosbag of CODa sequence 0 (RGB + estimated depth + camera info + tf) with the ROS dataloader, at the dataset's
# own 10 Hz. The playback speed is chosen when the bag is played (RATE in scripts/run_coda_example.sh), not here.
#
# Inside the daaam-ws container (or: just coda-bag [START [FRAMES [NAME]]]):
#   docker compose run --rm -T daaam-ws bash scripts/make_coda_bag.sh [START [FRAMES [NAME]]]
#
#   START   first frame (default 0)
#   FRAMES  number of frames (default 0 = up to the last frame of the sequence)
#   NAME    bag name without ".bag" (default coda_0_with_depth); the dataloader appends a time stamp, so the bag ends up
#           as $CODA_ROOT_DIR/NAME_<yyyymmdd_hhmmss>.bag (the writer needs about 6 frames per second)
#
# The depth maps (scripts/run_foundation_stereo_coda.py) must exist for every frame up to the end of the range: the
# loader pairs them with the images by sorted index.
#
# A closed bag gets the sidecar NAME_<time>.bag/daaam_replay.json: dataset kind, sequence, first frame, frame count, frame
# rate and time origin (the first stamp of $CODA_ROOT_DIR/timestamps/0.txt). scripts/run_coda_example.sh reads it to give
# the run's result its replay bundle; it is written through a temporary file and only after the bag is complete.
set -uo pipefail

START="${1:-0}"
FRAMES="${2:-0}"
NAME="${3:-coda_0_with_depth}"
: "${CODA_ROOT_DIR:?set CODA_ROOT_DIR (the compose file sets it to /data/CODa)}"
case "${START}${FRAMES}" in
  ''|*[!0-9]*) echo "START and FRAMES must be non-negative integers" >&2; exit 2 ;;
esac
case "${NAME}" in
  ''|*/*) echo "NAME is a bag name without a directory" >&2; exit 2 ;;
esac

STAMPS="${CODA_ROOT_DIR}/timestamps/0.txt"
if [ ! -f "${STAMPS}" ]; then
  echo "${STAMPS} is missing (timestamps/0.txt of the CODa download): the replay metadata of the bag needs the sequence's time origin" >&2
  exit 2
fi
TOTAL="$(grep -c . "${STAMPS}")"
ORIGIN="$(head -n 1 "${STAMPS}" | tr -d '[:space:]')"
if ! [[ "${ORIGIN}" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
  echo "the first line of ${STAMPS} is not a time stamp: '${ORIGIN}'" >&2
  exit 2
fi
if [ "${START}" -ge "${TOTAL}" ] || { [ "${FRAMES}" -gt 0 ] && [ $((START + FRAMES)) -gt "${TOTAL}" ]; }; then
  echo "frames ${START}..$((START + FRAMES - 1)) are outside sequence 0, which has ${TOTAL} frames" >&2
  exit 2
fi
if [ "${FRAMES}" -gt 0 ]; then COUNT="${FRAMES}"; else COUNT=$((TOTAL - START)); fi

# The ROS dataloader returns the frames start_frame .. end_frame-start_frame-1 (it compares start_frame + index with
# end_frame - start_frame), so a range that does not start at frame 0 needs end_frame = 2 * start + length. -1 = to the end.
if [ "${FRAMES}" -gt 0 ]; then END=$((2 * START + FRAMES)); else END=-1; fi

bags() { find "${CODA_ROOT_DIR}" -maxdepth 1 -type d -name "${NAME}_*.bag" 2>/dev/null | sort; }
before="$(bags)"

echo "Recording ${CODA_ROOT_DIR}/${NAME}_<time>.bag from frame ${START} ($([ "${FRAMES}" -gt 0 ] && echo "${FRAMES} frames" || echo "to the end of the sequence")): start_frame=${START} end_frame=${END}"
rc=0
ros2 launch daaam_ros dataloader_coda_with_depth.launch.yaml sequence:=0 dataset_path:="${CODA_ROOT_DIR}" \
  bag_path:="${CODA_ROOT_DIR}/${NAME}.bag" depth_source:=3d_raw_estimated start_frame:="${START}" end_frame:="${END}" || rc=$?

# the bag of this run is the new one; the dataloader writes metadata.yaml when it closes it: without it the bag is incomplete
bag="$(comm -13 <(echo "${before}") <(bags) | tail -n 1)"
if [ -n "${bag}" ] && [ -f "${bag}/metadata.yaml" ]; then
  sidecar="${bag}/daaam_replay.json"
  temporary="${bag}/.daaam_replay.json.$$.tmp"
  if ! printf '{"dataset_kind":"coda","sequence":"0","start_frame":%d,"frame_count":%d,"fps":10.0,"time_origin":%s}\n' \
      "${START}" "${COUNT}" "${ORIGIN}" > "${temporary}" || ! mv -f "${temporary}" "${sidecar}"; then
    rm -f "${temporary}"
    echo "Could not write ${sidecar}" >&2
    exit 1
  fi
  echo "Bag: ${bag} ($(du -sh "${bag}" | cut -f1))"
  echo "Replay metadata: ${sidecar} (frames ${START}..$((START + COUNT - 1)) of sequence 0)"
  exit 0
fi
echo "No complete bag was written (the dataloader exited with ${rc})" >&2
exit 1
