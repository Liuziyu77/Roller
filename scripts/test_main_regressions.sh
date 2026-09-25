#!/usr/bin/env bash
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_DIR="${LOG_DIR:-}"
if [[ "${1:-}" == --help ]]; then
  printf '%s\n' 'Run selected offline main-branch regressions (no API or Docker).' \
    'PYTHON_BIN selects Python >=3.10 with project dependencies installed.' \
    'Optional arguments replace the default unittest module list.' \
    'LOG_DIR optionally saves a new timestamped log (never overwrites old logs).'
  exit 0
fi
for value in "$REPO_DIR" "$PYTHON_BIN" "$LOG_DIR" "$@"; do
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || exit 2
done
cd "$REPO_DIR"
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$REPO_DIR/src:$REPO_DIR/tests"
modules=(test_actf test_codex_actf test_sft test_wire_trace test_collector test_resume test_pi test_streaming_traces test_wire_tool_sources test_runtime_regressions test_multimodal_integrity test_native_attachments test_tool_id_identity test_deepseek test_opencode test_attachment_recovery test_codex_integrity test_training_status test_conversation test_codex test_claude_code test_openclaw test_hermes test_tasks test_bundles test_run_lock test_compatibility_config)
[[ $# == 0 ]] || modules=("$@")
command=("$PYTHON_BIN" -m unittest "${modules[@]}" -q)
printf 'Running:'; printf ' %q' "${command[@]}"; printf '\n'
if [[ -n "$LOG_DIR" ]]; then
  mkdir -p "$LOG_DIR"
  log_file="$(mktemp "$LOG_DIR/main-regressions.$(date +%Y%m%dT%H%M%S).XXXXXX.log")"
  printf 'Log: %s\n' "$log_file"
  "${command[@]}" 2>&1 | tee "$log_file"
else
  "${command[@]}"
fi
