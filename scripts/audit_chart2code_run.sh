#!/usr/bin/env bash
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
RUN_DIR="${RUN_DIR:-$REPO_DIR/runs/chart2code-canary}"
MODE="${MODE:-audit}"
REPORT_ONLY="${REPORT_ONLY:-0}"
if [[ "${1:-}" == --help ]]; then
  printf '%s\n' 'Offline evidence audit; no Docker, API or execution of collected scripts.' \
    'RUN_DIR must contain chart2code/contracts.json and Roller run/sample metadata.' \
    'MODE=audit (default); REPORT_ONLY=1 cannot grant qualification.' \
    'MODE=export requires OUTPUT_DIR (new directory outside RUN_DIR).' \
    'PYTHON_BIN selects Python with Roller dependencies. Extra args go to the CLI.'
  exit 0
fi
for value in "$REPO_DIR" "$PYTHON_BIN" "$RUN_DIR" "$MODE" "${OUTPUT_DIR:-}" "$@"; do
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || exit 2
done
[[ "$REPORT_ONLY" == 0 || "$REPORT_ONLY" == 1 ]] || exit 2
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$REPO_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
command=("$PYTHON_BIN" -m roller_chart2code.export "$MODE" --run-dir "$RUN_DIR")
if [[ "$MODE" == export ]]; then
  : "${OUTPUT_DIR:?MODE=export requires OUTPUT_DIR}"
  command+=(--output "$OUTPUT_DIR")
fi
if [[ "$REPORT_ONLY" == 1 ]]; then command+=(--report-only); fi
command+=("$@")
printf 'Running:'; printf ' %q' "${command[@]}"; printf '\n'
"${command[@]}"
