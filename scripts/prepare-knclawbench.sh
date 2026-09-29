#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
bench_root="${1:-$repo_root/.benchmarks/knclawbench-v1.1}"
expected_revision="ca8af7f914f76082b4c27c21481da2495a5d5125"
image_tag="ghcr.io/openclaw/openclaw:2026.7.1-2"

command -v git >/dev/null
command -v docker >/dev/null

if [[ ! -e "$bench_root" ]]; then
  mkdir -p "$(dirname "$bench_root")"
  git init "$bench_root"
  git -C "$bench_root" remote add origin https://github.com/SKYLENAGE-AI/QwenClawBench.git
  git -C "$bench_root" fetch --depth 1 origin "$expected_revision"
  git -C "$bench_root" checkout --detach FETCH_HEAD
elif ! git -C "$bench_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "error: $bench_root exists but is not a Git checkout" >&2
  exit 1
fi

actual_revision="$(git -C "$bench_root" rev-parse HEAD)"
if [[ "$actual_revision" != "$expected_revision" ]]; then
  echo "error: expected QwenClawBench at $expected_revision, found $actual_revision" >&2
  exit 1
fi

if ! docker image inspect "$image_tag" >/dev/null 2>&1; then
  docker pull --platform linux/amd64 "$image_tag"
fi

printf 'QwenClawBench root: %s\n' "$(cd "$bench_root" && pwd)"
printf 'Revision: %s\n' "$actual_revision"
printf 'Harness image: %s\n' "$image_tag"
