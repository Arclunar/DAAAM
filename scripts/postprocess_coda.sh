#!/usr/bin/env bash
# Post-process finished DAAAM pipeline results. By default this uses CODa's
# output/coda/out_<UTC time>/; RESULT_ROOT selects another result root, such as
# output/tum_replay_fr1_room for a TUM sequence. The steps form two stages:
#   postprocess  1. sentence embeddings of the object descriptions  scripts/postprocess_scene_graph.py -> dsg_updated.json
#                2. place clustering into rooms / regions            cluster_places.launch.yaml          -> clustered_dsg.json
#   summarize    3. region summaries written by OpenAI (needs OPENAI_API_KEY, in the repo's .env (loaded by
#                daaam/__init__.py) or in the environment)           scripts/summarize_regions.py        -> clustered_dsg_with_summaries.json,
#                                                                                                           region_summaries.yaml
#
# Inside the daaam-ws container (or via the coda-* / tum-* just recipes):
#   docker compose run --rm -T -e HF_HOME=/data/hf_cache daaam-ws bash scripts/postprocess_coda.sh [--stage S] [--atomic] [RUN ...]
#   docker compose run --rm -T --no-deps daaam-ws bash scripts/postprocess_coda.sh --list
#
#   RUN      a result directory name under RESULT_ROOT (or an absolute path); for TUM this may be the outer tum-run
#            directory, whose inner out_* directory contains dsg.json; default: the newest actual result
#   --stage  postprocess (steps 1-2, the default), summarize (step 3 only; needs the clustered_dsg.json of an earlier
#            postprocess stage) or all (both, one after the other)
#   --summaries  the older spelling of --stage all
#   --atomic every step writes next to its final file under a .replay-job-<pid>- name, and a stage publishes its files by
#            rename only when all of them were written: a failure, a Ctrl+C or a termination leaves the existing outputs
#            exactly as they were and removes only this process's temporary files (the replay workspace runs jobs this way)
#   --list   list the results (newest first) and which of the steps are done, then stop
# Environment: RESULT_ROOT (optional result root; default: output/coda of the repository), HF_HOME (default /data/hf_cache:
#   sentence-t5-xl, 2.5 GB, is downloaded there once and every step that loads it must look in the same place),
#   CLUSTER_PLACES_CONFIG (optional dataset-specific cluster config), PLACES_LAYER (default TRAVERSABILITY),
#   SUMMARY_MODEL (default gpt-5-nano).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
export HF_HOME="${HF_HOME:-/data/hf_cache}"
ROOT="$(realpath -m "${RESULT_ROOT:-output/coda}")"

stage=""
stage_given=0
summaries=0
atomic=0
list=0
runs=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --summaries) summaries=1 ;;
    --atomic) atomic=1 ;;
    --list) list=1 ;;
    --stage)
      [ "$#" -ge 2 ] || { echo "--stage needs a value: postprocess, summarize or all" >&2; exit 2; }
      stage="$2"; stage_given=1; shift ;;
    --stage=*) stage="${1#--stage=}"; stage_given=1 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) runs+=("$1") ;;
  esac
  shift
done
if [ "${stage_given}" -eq 0 ]; then
  stage=postprocess
  [ "${summaries}" -eq 0 ] || stage=all
fi
case "${stage}" in
  postprocess|summarize|all) ;;
  *) echo "unknown stage '${stage}': use postprocess, summarize or all" >&2; exit 2 ;;
esac
if [ "${summaries}" -eq 1 ] && [ "${stage}" != all ]; then
  echo "--summaries means --stage all and cannot be combined with --stage ${stage}" >&2
  exit 2
fi

results_newest_first() {   # actual pipeline result directories, including legacy nested TUM layouts
  # Current CODa/TUM: ROOT/out_*/dsg.json. Legacy TUM: ROOT/out_*/out_*/dsg.json.
  # The generated names contain no spaces; the timestamp prefix sorts the actual results newest first.
  find "${ROOT}" -mindepth 2 -maxdepth 3 -type f -name dsg.json -printf '%T@ %h\n' 2>/dev/null \
    | sort -nr | cut -d' ' -f2-
}

resolve_result_dir() {
  local candidate="${1%/}" nested
  if [ -f "${candidate}/dsg.json" ]; then
    echo "${candidate}"
    return
  fi
  nested="$(find "${candidate}" -mindepth 2 -maxdepth 2 -type f -name dsg.json -printf '%T@ %h\n' 2>/dev/null \
    | sort -nr | head -n 1 | cut -d' ' -f2-)"
  [ -z "${nested}" ] || echo "${nested}"
}

if [ "${list}" -eq 1 ]; then
  echo "Results in ${ROOT} (newest first):"
  for d in $(results_newest_first); do
    frames="$(awk -F, '$1 == "process_frame" {print $2}' "${d}/performance_statistics.csv" 2>/dev/null)"
    printf '  %-20s %5s frames   embeddings %-3s  clustering %-3s  summaries %s\n' "$(basename "${d}")" "${frames:-?}" \
      "$([ -f "${d}/dsg_updated.json" ] && echo yes || echo no)" "$([ -f "${d}/clustered_dsg.json" ] && echo yes || echo no)" \
      "$([ -f "${d}/clustered_dsg_with_summaries.json" ] && echo yes || echo no)"
  done
  exit 0
fi

if [ "${stage}" != postprocess ] && [ -z "${OPENAI_API_KEY:-}" ] && ! grep -q '^OPENAI_API_KEY=.' .env 2>/dev/null; then
  echo "the region summaries are written by OpenAI: put OPENAI_API_KEY=... into .env in the repository directory" >&2
  exit 2
fi
if [ "${#runs[@]}" -eq 0 ]; then
  newest="$(results_newest_first | head -n 1)"
  [ -n "${newest}" ] || { echo "no result with a dsg.json in ${ROOT}: run the dataset pipeline first" >&2; exit 2; }
  runs=("${newest}")
fi

# Files this process created next to the final outputs; whatever way the script ends, exactly these are removed.
tracked=()
track() { tracked+=("$1"); }
cleanup() {
  local file
  for file in ${tracked[@]+"${tracked[@]}"}; do rm -f -- "${file}"; done
}
# bash runs a trap between commands, so a step (python, ros2 launch) is started in the background and waited for: then a
# SIGTERM to this script alone (that is what `docker stop` sends to a container's first process) stops the step at once
# and still runs the cleanup, instead of waiting for the step to finish.
step=0
stop_step() { [ "${step}" -eq 0 ] || kill -TERM "${step}" 2>/dev/null; }
run_step() {
  "$@" &
  step=$!
  wait "${step}"
  local status=$?
  step=0
  return "${status}"
}
trap cleanup EXIT
trap 'stop_step; exit 143' TERM
trap 'stop_step; exit 130' INT

tmp_name() { echo "${dir}/.replay-job-${$}-$1"; }

# publish_outputs SRC DST [SRC DST ...]: every DST is replaced by its SRC (a rename inside one directory), all of them or
# none. The previous files are copied aside first and put back if a rename fails; a DST that did not exist is removed again.
publish_outputs() {
  local -a srcs=() dsts=() baks=()
  local i j
  while [ "$#" -ge 2 ]; do srcs+=("$1"); dsts+=("$2"); shift 2; done
  for i in "${!srcs[@]}"; do
    if [ ! -s "${srcs[$i]}" ]; then
      echo "=== ${run}: ${srcs[$i]##*/} was not written" >&2
      return 1
    fi
  done
  for i in "${!dsts[@]}"; do
    baks[$i]=""
    if [ -e "${dsts[$i]}" ]; then
      baks[$i]="${dir}/.replay-job-${$}.${dsts[$i]##*/}.bak"
      track "${baks[$i]}"
      if ! cp -- "${dsts[$i]}" "${baks[$i]}"; then
        echo "=== ${run}: cannot back up ${dsts[$i]##*/}" >&2
        return 1
      fi
    fi
  done
  for i in "${!srcs[@]}"; do
    if ! mv -f -- "${srcs[$i]}" "${dsts[$i]}"; then
      echo "=== ${run}: cannot publish ${dsts[$i]##*/}; the previous files are put back" >&2
      for ((j = 0; j < i; j++)); do
        if [ -n "${baks[$j]}" ]; then mv -f -- "${baks[$j]}" "${dsts[$j]}"; else rm -f -- "${dsts[$j]}"; fi
      done
      return 1
    fi
  done
  for i in "${!baks[@]}"; do
    [ -z "${baks[$i]}" ] || rm -f -- "${baks[$i]}"
  done
  return 0
}

# Stage postprocess: sentence embeddings, then place clustering. Succeeds when dsg_updated.json and clustered_dsg.json are
# in place (with --atomic: published together, from files that only this stage wrote).
run_postprocess() {
  local updated="${dir}/dsg_updated.json" clustered="${dir}/clustered_dsg.json"
  local embed_args=(--data-dir "${dir}") cluster_args cluster_input="" cluster_output=""
  if [ "${atomic}" -eq 1 ]; then
    updated="$(tmp_name dsg_updated.json)"
    clustered="$(tmp_name clustered_dsg.json)"
    track "${updated}"
    track "${clustered}"
    embed_args+=(--output "${updated}")
  fi
  echo "=== ${run}: sentence embeddings"
  if ! run_step python scripts/postprocess_scene_graph.py "${embed_args[@]}" || [ ! -s "${updated}" ]; then
    echo "=== ${run}: the embedding step failed" >&2
    return 1
  fi
  echo "=== ${run}: place clustering"
  if [ "${PLACES_LAYER:-TRAVERSABILITY}" = TRAVERSABILITY ]; then
    if ! run_step python scripts/adapt_cluster_places.py validate --src "${updated}" --layer TRAVERSABILITY; then
      echo "=== ${run}: native place semantics are missing; clustering was not started" >&2
      return 1
    fi
  fi
  cluster_args=(data_dir:="${dir}" interactive:=false)
  if [ "${PLACES_LAYER:-TRAVERSABILITY}" != "TRAVERSABILITY" ]; then
    cluster_input="${dir}/.cluster_places_input.json"
    cluster_output="${dir}/.cluster_places_output.json"
    track "${cluster_input}"
    track "${cluster_output}"
    rm -f "${cluster_input}" "${cluster_output}"
    if ! run_step python scripts/adapt_cluster_places.py prepare --src "${updated}" --dst "${cluster_input}" --layer "${PLACES_LAYER}"; then
      echo "=== ${run}: could not adapt ${PLACES_LAYER} for clustering" >&2
      return 1
    fi
    cluster_args+=(src_dsg:="${cluster_input}" save_dsg:="${cluster_output}")
  elif [ "${atomic}" -eq 1 ]; then
    cluster_args+=(src_dsg:="${updated}" save_dsg:="${clustered}")
  fi
  if [ -n "${CLUSTER_PLACES_CONFIG:-}" ]; then
    cluster_args+=(cluster_places_config_path:="${CLUSTER_PLACES_CONFIG}")
  fi
  if ! run_step ros2 launch daaam_ros cluster_places.launch.yaml "${cluster_args[@]}"; then
    echo "=== ${run}: the clustering step failed" >&2
    return 1
  fi
  if [ -n "${cluster_output}" ]; then
    if ! run_step python scripts/adapt_cluster_places.py restore --src "${cluster_output}" --original "${updated}" \
      --dst "${clustered}" --layer "${PLACES_LAYER}"; then
      echo "=== ${run}: could not restore ${PLACES_LAYER} after clustering" >&2
      return 1
    fi
    rm -f "${cluster_input}" "${cluster_output}"
  fi
  if [ ! -s "${clustered}" ]; then
    echo "=== ${run}: the clustering step produced no clustered_dsg.json" >&2
    return 1
  fi
  if [ "${PLACES_LAYER:-TRAVERSABILITY}" = TRAVERSABILITY ]; then
    if ! run_step python scripts/adapt_cluster_places.py validate --src "${clustered}" --layer TRAVERSABILITY --require-rooms; then
      echo "=== ${run}: native region clustering failed validation" >&2
      return 1
    fi
  fi
  if [ "${atomic}" -eq 1 ]; then
    publish_outputs "${updated}" "${dir}/dsg_updated.json" "${clustered}" "${dir}/clustered_dsg.json" || return 1
  fi
  return 0
}

# Stage summarize: region summaries of the clustered graph. Succeeds when clustered_dsg_with_summaries.json (and, with
# --atomic, region_summaries.yaml) are in place.
run_summaries() {
  local graph="${dir}/clustered_dsg_with_summaries.json" yaml="${dir}/region_summaries.yaml" extra=()
  if [ ! -s "${dir}/clustered_dsg.json" ]; then
    echo "=== ${run}: the summary step needs clustered_dsg.json: run the postprocess stage first" >&2
    return 1
  fi
  if [ "${atomic}" -eq 1 ]; then
    graph="$(tmp_name clustered_dsg_with_summaries.json)"
    yaml="$(tmp_name region_summaries.yaml)"
    track "${graph}"
    track "${yaml}"
    extra=(--summary-output "${yaml}" --scene-graph-output "${graph}")
  fi
  echo "=== ${run}: region summaries (${SUMMARY_MODEL:-gpt-5-nano})"
  if ! run_step python scripts/summarize_regions.py --data-dir "${dir}" --model-name "${SUMMARY_MODEL:-gpt-5-nano}" --n-samples 20 \
    --places-layer "${PLACES_LAYER:-TRAVERSABILITY}" ${extra[@]+"${extra[@]}"} || [ ! -s "${graph}" ]; then
    echo "=== ${run}: the summary step failed" >&2
    return 1
  fi
  if [ "${atomic}" -eq 1 ]; then
    if ! publish_outputs "${graph}" "${dir}/clustered_dsg_with_summaries.json" "${yaml}" "${dir}/region_summaries.yaml"; then
      echo "=== ${run}: the summary step failed" >&2
      return 1
    fi
  fi
  return 0
}

failed=0
for run in "${runs[@]}"; do
  case "${run}" in /*) candidate="${run%/}" ;; *) candidate="${ROOT}/${run%/}" ;; esac
  dir="$(resolve_result_dir "${candidate}")"
  if [ -z "${dir}" ]; then
    echo "=== ${run}: no dsg.json in ${candidate} or a direct out_* child, skipped" >&2
    failed=$((failed + 1))
    continue
  fi
  ok=1
  if [ "${stage}" != summarize ]; then run_postprocess || ok=0; fi
  if [ "${ok}" -eq 1 ] && [ "${stage}" != postprocess ]; then run_summaries || ok=0; fi
  if [ "${ok}" -eq 0 ]; then
    failed=$((failed + 1))
    continue
  fi
  made=""
  for f in dsg_updated clustered_dsg clustered_dsg_with_summaries; do
    if [ -f "${dir}/${f}.json" ]; then made="${made} ${f}.json"; fi
  done
  echo "=== ${run}: done (${made# })"
done
[ "${failed}" -eq 0 ] || { echo "${failed} result(s) failed" >&2; exit 1; }
