#!/usr/bin/env bash
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ "${1:-}" == --help ]]; then
  printf '%s\n' 'Offline Chart2Code synthetic recorder/acceptance tests; no API or Docker.' \
    'PYTHON_BIN selects Python with Roller dependencies. Optional args select unittest modules.'
  exit 0
fi
for value in "$REPO_DIR" "$PYTHON_BIN" "$@"; do
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || exit 2
done
cd "$REPO_DIR"
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$REPO_DIR/src:$REPO_DIR/tests"
export CHART2CODE_SYSTEM_PYTHON="$PYTHON_BIN"
command=("$PYTHON_BIN" -m unittest)
if [[ $# == 0 ]]; then
  command+=(discover -s tests/chart2code -p 'test_*.py' -q)
else
  command+=("$@" -q)
fi
printf 'Running:'; printf ' %q' "${command[@]}"; printf '\n'
"${command[@]}"
