#!/usr/bin/env bash
# Run the whole CODa sequence 0 in N chunks: for every chunk record its rosbag (scripts/make_coda_bag.sh) and run DAAAM +
# Hydra on it (scripts/run_coda_example.sh), one after the other in fresh processes. Why: on 15.9 GB RAM + 4 GB swap one
# run over all 8213 frames fills the memory at about frame 3600 and the kernel kills Hydra, while chunks of about 2050
# frames fit (the processes of a chunk are gone before the next one starts). Every chunk gives its own scene graph, all
# in the same world frame; they are not merged.
#
# Inside the daaam-ws container (or: just coda-chunks [N [RATE [CHUNK ...]]]):
#   docker compose run --rm -T -e RATE=0.3 daaam-ws bash scripts/run_coda_chunks.sh [N [CHUNK ...]]
#
#   N      number of chunks (default 4); the frames are split evenly, the last chunk takes the remainder
#   CHUNK  chunk numbers to run (default: all), e.g. "3 4" to run only the last two
# Environment: RATE (bag playback speed, default 0.3: the bags are 10 Hz, see run_coda_example.sh), LABELS (size of the
#   pseudo label space; default 1000 for N >= 4 and 5000 otherwise: a chunk of 2050 frames uses about 600 labels),
#   TOTAL (number of frames; default: the depth maps in $CODA_ROOT_DIR/3d_raw_estimated/cam0/0).
#
# The bag of a chunk is named after its frame range, $CODA_ROOT_DIR/coda_0_f<first>-<end>_<time>.bag, and a bag that
# exists already for exactly that range is reused when it is complete (metadata.yaml) and carries the replay sidecar
# daaam_replay.json that scripts/make_coda_bag.sh writes; an older bag without it is recorded again, so that every
# chunk's result gets its replay bundle. Per chunk the logs are in output/coda_run_chunk<k>of<N>/ (bag.log, console.log,
# launch.log, play.log) and the results in output/coda/out_<time>/, all inside the repository (the bags stay under
# $CODA_ROOT_DIR); one line per chunk goes to output/coda_chunks.tsv.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
: "${CODA_ROOT_DIR:?set CODA_ROOT_DIR (the compose file sets it to /data/CODa)}"

N="${1:-4}"
[ "$#" -gt 0 ] && shift
WANT="$*"
case "${N}" in
  ''|*[!0-9]*|0) echo "N must be a positive integer" >&2; exit 2 ;;
esac
DEPTH_DIR="${CODA_ROOT_DIR}/3d_raw_estimated/cam0/0"
TOTAL="${TOTAL:-$(find "${DEPTH_DIR}" -maxdepth 1 -name '*.png' 2>/dev/null | wc -l)}"
if [ "${TOTAL}" -lt "${N}" ]; then
  echo "only ${TOTAL} depth maps in ${DEPTH_DIR}: estimate the depth first (just coda-depth)" >&2
  exit 2
fi
BASE=$((TOTAL / N))
RATE="${RATE:-0.3}"
LABELS="${LABELS:-$([ "${N}" -ge 4 ] && echo 1000 || echo 5000)}"
OUT_ROOT="$(pwd)/output"
TSV="${OUT_ROOT}/coda_chunks.tsv"
mkdir -p "${OUT_ROOT}"

newest_complete_bag() {   # newest bag of the chunk $1 that the dataloader closed (metadata.yaml) with its replay sidecar
  local b
  # the names are made by the dataloader (no spaces), and ls -t gives the newest first
  # shellcheck disable=SC2045
  for b in $(ls -dt "${CODA_ROOT_DIR}/$1"_*.bag 2>/dev/null); do
    if [ -f "${b}/metadata.yaml" ] && [ -f "${b}/daaam_replay.json" ]; then echo "${b}"; return; fi
  done
}

echo "CODa sequence 0: ${TOTAL} frames in ${N} chunks of about ${BASE}; playback rate ${RATE}, ${LABELS} labels"
failed=0
results=()
for ((k = 1; k <= N; k++)); do
  if [ -n "${WANT}" ]; then case " ${WANT} " in *" ${k} "*) ;; *) continue ;; esac; fi
  first=$(((k - 1) * BASE))
  if [ "${k}" -lt "${N}" ]; then length=${BASE}; else length=$((TOTAL - first)); fi
  name="coda_0_f${first}-$((first + length))"
  out="${OUT_ROOT}/coda_run_chunk${k}of${N}"
  mkdir -p "${out}"

  bag="$(newest_complete_bag "${name}")"
  if [ -z "${bag}" ]; then
    echo "=== chunk ${k}/${N}: recording the bag of frames ${first}..$((first + length - 1)) (log: ${out}/bag.log)"
    bash "${JELLYMAP_ROOT:?JELLYMAP_ROOT is not set}/sm-lab/datasets/scripts/make_coda_bag.sh" "${first}" "${length}" "${name}" > "${out}/bag.log" 2>&1
    bag="$(newest_complete_bag "${name}")"
  fi
  if [ -z "${bag}" ]; then
    echo "=== chunk ${k}/${N}: no complete bag, see ${out}/bag.log" >&2
    failed=$((failed + 1))
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "${k}" "${N}" "${first}" "$((first + length))" "no-bag" 0 "" >> "${TSV}"
    continue
  fi

  echo "=== chunk ${k}/${N}: running the pipeline on ${bag} (log: ${out}/console.log)"
  rc=0
  RATE="${RATE}" LABELS="${LABELS}" OUT="${out}" bash scripts/run_coda_example.sh "${bag}" > "${out}/console.log" 2>&1 || rc=$?
  result="$(grep -o 'Results: .*' "${out}/console.log" | tail -n 1 | sed 's/^Results: //')"
  frames="$(grep -c 'process_frame - execution time' "${out}/launch.log" 2>/dev/null || true)"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "${k}" "${N}" "${first}" "$((first + length))" "${rc}" "${frames:-0}" "${result}" >> "${TSV}"
  echo "=== chunk ${k}/${N}: exit code ${rc}, ${frames:-0} frames processed, results: ${result:-none}"
  [ "${rc}" -eq 0 ] || failed=$((failed + 1))
  [ -z "${result}" ] || results+=("$(basename "${result}")")
done

echo
echo "Chunk results (listed in ${TSV}): ${results[*]:-none}"
[ "${#results[@]}" -eq 0 ] || echo "Next: just coda-post ${results[*]}   (then just coda-summarize ${results[*]} for the region summaries)"
[ "${failed}" -eq 0 ] || { echo "${failed} chunk(s) failed" >&2; exit 1; }
