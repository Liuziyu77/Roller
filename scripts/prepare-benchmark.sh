#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 2
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if (($# < 1)); then
  fail "usage: scripts/prepare-benchmark.sh BENCHMARK [ROOT]"
fi

benchmark="$1"
shift

case "$benchmark" in
  wildclawbench | pinchbench | skillsbench) ;;
  *) fail "unsupported benchmark: $benchmark (expected wildclawbench, pinchbench, or skillsbench)" ;;
esac

exec "$script_dir/prepare-$benchmark.sh" "$@"
