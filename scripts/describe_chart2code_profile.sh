#!/usr/bin/env bash
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PROFILE="${PROFILE:-single-render-v2}"
REFERENCE="${REFERENCE:-inputs/reference.png}"
for value in "$REPO_DIR" "$PYTHON_BIN" "$PROFILE" "$REFERENCE" "$@"; do
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || exit 2
done
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$REPO_DIR/src"
command=("$PYTHON_BIN" -m roller_chart2code.profiles --profile "$PROFILE" --reference "$REFERENCE" "$@")
printf 'Running:'; printf ' %q' "${command[@]}"; printf '\n'
"${command[@]}"
