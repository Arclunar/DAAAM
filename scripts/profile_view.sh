#!/usr/bin/env bash
# Keep the extension: vizviewer uses it to select Perfetto JSON vs standalone HTML.
set -euo pipefail
report="${1:?Usage: profile_view.sh REPORT [PORT]}"
port="${2:-9001}"
case "$report" in
  /*) ;;
  *) report="${WARMOON_CALLER_DIR:-$PWD}/$report" ;;
esac
report=$(realpath -e -- "$report")
[ -f "$report" ] || { echo "Report is not a file: $report" >&2; exit 2; }
case "$report" in
  *.html|*.json|*.gz) ;;
  *) echo "Expected a VizTracer JSON/GZ or a standalone HTML report" >&2; exit 2 ;;
esac
target="/tmp/profile-report.${report##*.}"
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
exec docker compose run --rm -T --no-deps -v "$report:$target:ro" daaam-ws   vizviewer --server_only --port "$port" "$target"
