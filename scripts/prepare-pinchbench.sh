#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
bench_root="${1:-$repo_root/.benchmarks/pinchbench}"

command -v git >/dev/null
command -v docker >/dev/null

if [[ ! -e "$bench_root" ]]; then
  mkdir -p "$(dirname "$bench_root")"
  git clone --depth 1 https://github.com/pinchbench/skill.git "$bench_root"
elif [[ ! -d "$bench_root/.git" ]]; then
  echo "error: $bench_root exists but is not a Git checkout" >&2
  exit 1
fi

docker pull --platform linux/amd64 ghcr.io/openclaw/openclaw:2026.7.1-2

printf 'PinchBench root: %s\n' "$(cd "$bench_root" && pwd)"
printf 'Revision: %s\n' "$(git -C "$bench_root" rev-parse HEAD)"
printf 'Harness image: ghcr.io/openclaw/openclaw:2026.7.1-2\n'
