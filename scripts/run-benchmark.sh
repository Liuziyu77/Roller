#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 2
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

if (($# == 0)); then
  fail "usage: scripts/run-benchmark.sh --config FILE [--extra-config FILE_OR_JSON] [--dry-run]"
fi

exec "${roller_cmd[@]}" bench run "$@"
