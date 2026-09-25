#!/usr/bin/env bash
set -euo pipefail
system_python="${CHART2CODE_SYSTEM_PYTHON:-/usr/bin/python3}"
if [[ "$#" -eq 1 ]]; then
  case "$1" in
    outputs/plot.py|*/outputs/plot.py)
      recorder="$(dirname -- "${BASH_SOURCE[0]}")/render_recorder.py"
      exec "$system_python" "$recorder" "$1"
      ;;
  esac
fi
exec "$system_python" "$@"
