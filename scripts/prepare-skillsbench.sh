#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
bench_root="${1:-$repo_root/.benchmarks/skillsbench-v1.1}"
expected_revision="b63b7b2850226b6aa4fb5929a8c1ac7bc4d9a6af"

command -v git >/dev/null
command -v docker >/dev/null

if [[ ! -e "$bench_root" ]]; then
  mkdir -p "$(dirname "$bench_root")"
  git clone --branch v1.1 --depth 1 \
    https://github.com/benchflow-ai/skillsbench.git "$bench_root"
elif [[ ! -d "$bench_root/.git" ]]; then
  echo "error: $bench_root exists but is not a Git checkout" >&2
  exit 1
fi

actual_revision="$(git -C "$bench_root" rev-parse HEAD)"
if [[ "$actual_revision" != "$expected_revision" ]]; then
  echo "error: expected SkillsBench v1.1 at $expected_revision, found $actual_revision" >&2
  exit 1
fi

docker build -t roller-opencode:1.18.30 "$repo_root/docker/opencode"

printf 'SkillsBench root: %s\n' "$(cd "$bench_root" && pwd)"
printf 'Revision: %s\n' "$actual_revision"
printf 'Base image: roller-opencode:1.18.30\n'
