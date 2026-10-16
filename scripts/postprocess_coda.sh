#!/usr/bin/env bash
# Post-process finished DAAAM pipeline results. By default this uses CODa's
# output/coda/out_<UTC time>/; RESULT_ROOT selects another result root, such as
# output/tum_replay_fr1_room for a TUM sequence:
#   1. sentence embeddings of the object descriptions       scripts/postprocess_scene_graph.py     -> dsg_updated.json
#   2. place clustering into rooms / regions                 cluster_places.launch.yaml             -> clustered_dsg.json
#   3. region summaries written by OpenAI (only with --summaries; needs OPENAI_API_KEY, in the repo's .env (loaded by
#      daaam/__init__.py) or in the environment)             scripts/summarize_regions.py           -> clustered_dsg_with_summaries.json,
#                                                                                                   region_summaries.yaml
#
# Inside the daaam-ws container (or via the coda-* / tum-* just recipes):
#   docker compose run --rm -T -e HF_HOME=/data/hf_cache daaam-ws bash scripts/postprocess_coda.sh [--summaries] [RUN ...]
#   docker compose run --rm -T --no-deps daaam-ws bash scripts/postprocess_coda.sh --list
#
#   RUN      a result directory name under RESULT_ROOT (or an absolute path); for TUM this may be the outer tum-run
#            directory, whose inner out_* directory contains dsg.json; default: the newest actual result
#   --list   list the results (newest first) and which of the steps are done, then stop
# Environment: RESULT_ROOT (optional result root; default: CODa output), HF_HOME (default /data/hf_cache:
#   sentence-t5-xl, 2.5 GB, is downloaded there once and every step that loads it must look in the same place),
#   CLUSTER_PLACES_CONFIG (optional dataset-specific cluster config), PLACES_LAYER (default TRAVERSABILITY),
#   SUMMARY_MODEL (default gpt-5-nano).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
export HF_HOME="${HF_HOME:-/data/hf_cache}"
if [ -n "${RESULT_ROOT:-}" ]; then
  ROOT="$(realpath -m "${RESULT_ROOT}")"
else
  : "${CODA_ROOT_DIR:?set CODA_ROOT_DIR (the compose file sets it to /data/CODa), or set RESULT_ROOT}"
  ROOT="$(realpath -m "${CODA_ROOT_DIR}/../output/coda")"
fi

summaries=0
list=0
runs=()
for arg in "$@"; do
  case "${arg}" in
    --summaries) summaries=1 ;;
    --list) list=1 ;;
    -*) echo "unknown option ${arg}" >&2; exit 2 ;;
    *) runs+=("${arg}") ;;
  esac
done

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

if [ "${summaries}" -eq 1 ] && [ -z "${OPENAI_API_KEY:-}" ] && ! grep -q '^OPENAI_API_KEY=.' .env 2>/dev/null; then
  echo "the region summaries are written by OpenAI: put OPENAI_API_KEY=... into .env in the repository directory" >&2
  exit 2
fi
if [ "${#runs[@]}" -eq 0 ]; then
  newest="$(results_newest_first | head -n 1)"
  [ -n "${newest}" ] || { echo "no result with a dsg.json in ${ROOT}: run the dataset pipeline first" >&2; exit 2; }
  runs=("${newest}")
fi

failed=0
for run in "${runs[@]}"; do
  case "${run}" in /*) candidate="${run%/}" ;; *) candidate="${ROOT}/${run%/}" ;; esac
  dir="$(resolve_result_dir "${candidate}")"
  if [ -z "${dir}" ]; then
    echo "=== ${run}: no dsg.json in ${candidate} or a direct out_* child, skipped" >&2
    failed=$((failed + 1))
    continue
  fi
  echo "=== ${run}: sentence embeddings"
  if ! python scripts/postprocess_scene_graph.py --data-dir "${dir}" || [ ! -f "${dir}/dsg_updated.json" ]; then
    echo "=== ${run}: the embedding step failed" >&2; failed=$((failed + 1)); continue
  fi
  echo "=== ${run}: place clustering"
	cluster_args=(data_dir:="${dir}" interactive:=false)
	cluster_input=""
	cluster_output=""
	cleanup_cluster_files() {
		[ -z "${cluster_input}" ] || rm -f "${cluster_input}"
		[ -z "${cluster_output}" ] || rm -f "${cluster_output}"
	}
	if [ "${PLACES_LAYER:-TRAVERSABILITY}" != "TRAVERSABILITY" ]; then
		cluster_input="${dir}/.cluster_places_input.json"
		cluster_output="${dir}/.cluster_places_output.json"
		rm -f "${cluster_input}" "${cluster_output}"
		if ! python scripts/adapt_cluster_places.py prepare --src "${dir}/dsg_updated.json" --dst "${cluster_input}" --layer "${PLACES_LAYER}"; then
			echo "=== ${run}: could not adapt ${PLACES_LAYER} for clustering" >&2
			failed=$((failed + 1))
			continue
		fi
		cluster_args+=(src_dsg:="${cluster_input}" save_dsg:="${cluster_output}")
	fi
	if [ -n "${CLUSTER_PLACES_CONFIG:-}" ]; then
		cluster_args+=(cluster_places_config_path:="${CLUSTER_PLACES_CONFIG}")
	fi
	if ! ros2 launch daaam_ros cluster_places.launch.yaml "${cluster_args[@]}"; then
		cleanup_cluster_files
    echo "=== ${run}: the clustering step failed" >&2; failed=$((failed + 1)); continue
  fi
	if [ -n "${cluster_output}" ]; then
		if ! python scripts/adapt_cluster_places.py restore --src "${cluster_output}" --original "${dir}/dsg_updated.json" \
			--dst "${dir}/clustered_dsg.json" --layer "${PLACES_LAYER}"; then
			cleanup_cluster_files
			echo "=== ${run}: could not restore ${PLACES_LAYER} after clustering" >&2
			failed=$((failed + 1))
			continue
		fi
		cleanup_cluster_files
	fi
	if [ ! -f "${dir}/clustered_dsg.json" ]; then
		echo "=== ${run}: the clustering step produced no clustered_dsg.json" >&2; failed=$((failed + 1)); continue
	fi
  if [ "${summaries}" -eq 1 ]; then
    echo "=== ${run}: region summaries (${SUMMARY_MODEL:-gpt-5-nano})"
		if ! python scripts/summarize_regions.py --data-dir "${dir}" --model-name "${SUMMARY_MODEL:-gpt-5-nano}" --n-samples 20 \
			--places-layer "${PLACES_LAYER:-TRAVERSABILITY}" \
      || [ ! -f "${dir}/clustered_dsg_with_summaries.json" ]; then
      echo "=== ${run}: the summary step failed" >&2; failed=$((failed + 1)); continue
    fi
  fi
  made=""
  for f in dsg_updated clustered_dsg clustered_dsg_with_summaries; do
    if [ -f "${dir}/${f}.json" ]; then made="${made} ${f}.json"; fi
  done
  echo "=== ${run}: done (${made# })"
done
[ "${failed}" -eq 0 ] || { echo "${failed} result(s) failed" >&2; exit 1; }
