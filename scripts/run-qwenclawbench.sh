#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 2
}

require_env_value() {
  local name="$1"
  [[ "$name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] ||
    fail "invalid environment variable name: $name"
  [[ -n "${!name:-}" ]] || fail "$name is not set or is empty"
}

append_csv_filters() {
  local flag="$1"
  local csv="$2"
  local item
  local -a values=()

  [[ -n "$csv" ]] || return 0
  IFS=',' read -r -a values <<<"$csv"
  for item in "${values[@]}"; do
    item="${item#"${item%%[![:space:]]*}"}"
    item="${item%"${item##*[![:space:]]}"}"
    [[ -n "$item" ]] && export_args+=("$flag" "$item")
  done
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"

if [[ -n "${ROLLER_BIN:-}" ]]; then
  if [[ "$ROLLER_BIN" == */* ]]; then
    [[ -x "$ROLLER_BIN" ]] || fail "ROLLER_BIN is not executable: $ROLLER_BIN"
  else
    command -v "$ROLLER_BIN" >/dev/null 2>&1 ||
      fail "ROLLER_BIN is not on PATH: $ROLLER_BIN"
  fi
  roller_cmd=("$ROLLER_BIN")
elif [[ -x "$repo_root/.venv/bin/roller" ]]; then
  roller_cmd=("$repo_root/.venv/bin/roller")
elif command -v roller >/dev/null 2>&1; then
  roller_cmd=("roller")
elif command -v python3 >/dev/null 2>&1; then
  export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"
  roller_cmd=("python3" "-m" "roller")
else
  fail "Roller is not installed; set ROLLER_BIN or create .venv"
fi

: "${QWENCLAW_ROOT:?set QWENCLAW_ROOT to the QwenClawBench checkout}"
: "${MODEL:?set MODEL to the model identifier used by the selected Harness}"

[[ -d "$QWENCLAW_ROOT" ]] ||
  fail "QWENCLAW_ROOT is not a directory: $QWENCLAW_ROOT"

harness="${HARNESS:-openclaw}"
case "$harness" in
  openclaw | codex | claude-code | hermes | opencode | deepseek | pi) ;;
  *) fail "unsupported HARNESS: $harness" ;;
esac

parallel="${PARALLEL:-1}"
retries="${RETRIES:-0}"
runs_per_task="${RUNS_PER_TASK:-3}"
output_root="${OUTPUT_ROOT:-/tmp/qwenclawbench-runs}"
run_name="${RUN_NAME:-qwenclawbench-${harness}-$(date -u +%Y%m%d_%H%M%S)}"
model_config="${MODEL_CONFIG:-$repo_root/examples/config/openclaw/models.json}"
max_steps="${MAX_STEPS:-}"
require_judge="${QWENCLAW_REQUIRE_JUDGE:-1}"

[[ "$run_name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$ ]] ||
  fail "RUN_NAME must use 1-100 ASCII letters, digits, dots, underscores, or hyphens"

case "$require_judge" in
  0) ;;
  1)
    require_env_value QWENCLAW_JUDGE_BASE_URL
    require_env_value QWENCLAW_JUDGE_API_KEY
    require_env_value QWENCLAW_JUDGE_MODEL
    ;;
  *) fail "QWENCLAW_REQUIRE_JUDGE must be 0 or 1" ;;
esac

tasks_dir="$(mktemp -d "${TMPDIR:-/tmp}/roller-qwenclawbench.XXXXXX")"
tasks_file="$tasks_dir/tasks.jsonl"
trap 'rm -rf -- "$tasks_dir"' EXIT

collect_args=(
  "${roller_cmd[@]}" collect
  --harness "$harness"
  --tasks "$tasks_file"
  --model "$MODEL"
  --task-timeouts
  --parallel "$parallel"
  --retries "$retries"
  --output "$output_root"
  --run-name "$run_name"
)

if [[ "$harness" == "openclaw" ]]; then
  [[ -f "$model_config" ]] || fail "MODEL_CONFIG is not a file: $model_config"
  collect_args+=(--model-config "$model_config")
else
  api_base="${API_BASE:-}"
  if [[ -z "${API_KEY_ENV:-}" ]]; then
    case "$harness" in
      claude-code) api_key_env="ANTHROPIC_API_KEY" ;;
      deepseek) api_key_env="DEEPSEEK_API_KEY" ;;
      *) api_key_env="OPENAI_API_KEY" ;;
    esac
  else
    api_key_env="$API_KEY_ENV"
  fi
  [[ -n "$api_base" ]] || fail "set API_BASE when HARNESS=$harness"
  require_env_value "$api_key_env"
  collect_args+=(--api-base "$api_base" --api-key-env "$api_key_env")
fi

if [[ -n "$max_steps" ]]; then
  case "$harness" in
    claude-code) collect_args+=(--claude-max-turns "$max_steps") ;;
    hermes) collect_args+=(--hermes-max-iterations "$max_steps") ;;
    opencode) collect_args+=(--opencode-max-steps "$max_steps") ;;
    deepseek) collect_args+=(--deepseek-max-steps "$max_steps") ;;
    pi) collect_args+=(--pi-max-steps "$max_steps") ;;
    *)
      printf 'warning: MAX_STEPS is not supported by the %s CLI and is ignored\n' \
        "$harness" >&2
      ;;
  esac
fi

export_args=(
  "${roller_cmd[@]}" bench export qwenclawbench "$QWENCLAW_ROOT"
  --runs-per-task "$runs_per_task"
  --output "$tasks_file"
)
append_csv_filters --task-id "${TASK_IDS:-}"
append_csv_filters --category "${CATEGORIES:-}"

"${export_args[@]}"

printf 'Running QwenClawBench with %s on %s\n' "$harness" "$MODEL"

set +e
"${collect_args[@]}" "$@"
collect_status=$?
set -e

run_dir="${output_root%/}/$run_name"
report_status=0
if [[ -f "$run_dir/info.json" ]]; then
  set +e
  "${roller_cmd[@]}" bench report "$run_dir" --output "$run_dir/report.json"
  report_status=$?
  set -e
fi

((collect_status == 0)) || exit "$collect_status"
exit "$report_status"
