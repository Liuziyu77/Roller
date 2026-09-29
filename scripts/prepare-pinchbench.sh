#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
bench_root="${1:-$repo_root/.benchmarks/pinchbench}"
expected_revision="819384ae830492365b8363fc26bc2602e73f216d"

command -v git >/dev/null
command -v docker >/dev/null

if [[ ! -e "$bench_root" ]]; then
  mkdir -p "$(dirname "$bench_root")"
  git init "$bench_root"
  git -C "$bench_root" remote add origin https://github.com/pinchbench/skill.git
  git -C "$bench_root" fetch --depth 1 origin "$expected_revision"
  git -C "$bench_root" checkout --detach FETCH_HEAD
elif ! git -C "$bench_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "error: $bench_root exists but is not a Git checkout" >&2
  exit 1
fi

actual_revision="$(git -C "$bench_root" rev-parse HEAD)"
if [[ "$actual_revision" != "$expected_revision" ]]; then
  echo "error: expected PinchBench at $expected_revision, found $actual_revision" >&2
  exit 1
fi

image_tag="ghcr.io/openclaw/openclaw:2026.7.1-2"
if ! docker image inspect "$image_tag" >/dev/null 2>&1; then
  docker pull --platform linux/amd64 "$image_tag"
fi

printf 'PinchBench root: %s\n' "$(cd "$bench_root" && pwd)"
printf 'Revision: %s\n' "$actual_revision"
printf 'Harness image: %s\n' "$image_tag"
