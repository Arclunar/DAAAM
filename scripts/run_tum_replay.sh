#!/usr/bin/env bash
set -uo pipefail
cd -- "${DAAAM_REPO_DIR:-$(dirname -- "${BASH_SOURCE[0]}")/..}"
frames="${1:-298}"
seq="${2:-fr1_desk}"
name=tum_replay
[ "$seq" = fr1_desk ] || name="tum_replay_$seq"
marker=$(mktemp)
trap 'rm -f -- "$marker"' EXIT
rc=0
docker compose run --rm -T -e "SEQ=$seq" -e "NAME=$name" -e REPLAY_EVERY=5 \
  -e REFERENCE_MESH -e REFERENCE_MESH_CACHE -e VIZTRACER -e PYINSTRUMENT daaam-ws bash scripts/run_replay.sh "$frames" || rc=$?
out="${DAAAM_OUTPUT_DIR:-output}"   # results on the host; compose mounts it at output/ in the container
result=$(find "$out/$name" -mindepth 1 -maxdepth 1 -type d -name 'out_*' -newer "$marker" -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n 1 | cut -d' ' -f2-)
if [ -n "$result" ]; then
  case "$result" in /*) ;; *) result="$PWD/$result" ;; esac
  echo "结果目录: $result"
  [ ! -f "$result/replay/manifest.json" ] || echo "回看: just replay-web"
  if [ -f "$result/replay.rrd" ]; then
    echo "Rerun 录制: $result/replay.rrd"
    echo "播放: just rerun-play '$result/replay.rrd'"
  fi
fi
exit "$rc"