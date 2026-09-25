#!/usr/bin/env bash
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ "${1:-}" == --help ]]; then
  printf '%s\n' 'Run selected offline main-branch regressions (no API or Docker).' \
    'PYTHON_BIN selects Python >=3.10 with project dependencies installed.' \
    'Optional arguments replace the default unittest module list.'
  exit 0
fi
for value in "$REPO_DIR" "$PYTHON_BIN"; do
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || exit 2
done
cd "$REPO_DIR"
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$REPO_DIR/src:$REPO_DIR/tests"
modules=(test_actf test_codex_actf test_sft test_wire_trace test_collector test_resume test_pi test_streaming_traces)
[[ $# == 0 ]] || modules=("$@")
command=("$PYTHON_BIN" -m unittest "${modules[@]}" -q)
printf 'Running:'; printf ' %q' "${command[@]}"; printf '\n'
"${command[@]}"
