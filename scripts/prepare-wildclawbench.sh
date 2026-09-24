#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 2
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
bench_root="${1:-$repo_root/.benchmarks/wildclawbench}"
harness="${WILDCLAW_HARNESS:-openclaw}"

command -v git >/dev/null || fail "git is required"
command -v docker >/dev/null || fail "docker is required"
command -v hf >/dev/null || fail "the Hugging Face CLI is required (pip install huggingface_hub)"

if [[ ! -e "$bench_root" ]]; then
  mkdir -p "$(dirname "$bench_root")"
  git clone --depth 1 https://github.com/InternLM/WildClawBench.git "$bench_root"
elif [[ ! -d "$bench_root/.git" ]]; then
  fail "$bench_root exists but is not a Git checkout"
fi

case "$harness" in
  openclaw)
    image_file="Images/wildclawbench-ubuntu_v1.3.tar"
    image_tag="wildclawbench-ubuntu:v1.3"
    ;;
  codex)
    image_file="Images/wildclawbench-codex-ubuntu_v0.0.tar"
    image_tag="wildclawbench-codex-ubuntu:v0.0"
    ;;
  claude-code)
    image_file="Images/wildclawbench-claudecode-ubuntu_v0.2-patched.tar"
    image_tag="wildclawbench-claudecode-ubuntu:v0.2"
    ;;
  hermes)
    image_file="Images/wildclawbench-hermes-agent-v0.5.tar.gz"
    image_tag="wildclawbench-hermes-agent:v0.5"
    ;;
  *) fail "WILDCLAW_HARNESS must be openclaw, codex, claude-code, or hermes" ;;
esac

if [[ ! -d "$bench_root/workspace" ]]; then
  hf download internlm/WildClawBench \
    --repo-type dataset --include 'workspace/**' --local-dir "$bench_root"
fi

if ! docker image inspect "$image_tag" >/dev/null 2>&1; then
  download_root="$(mktemp -d "${TMPDIR:-/tmp}/roller-wildclaw-image.XXXXXX")"
  trap 'rm -rf -- "$download_root"' EXIT
  hf download internlm/WildClawBench "$image_file" \
    --repo-type dataset --local-dir "$download_root"
  docker load -i "$download_root/$image_file"
fi

if [[ "${WILDCLAW_SKIP_ASSET_PREPARE:-0}" != "1" ]]; then
  command -v yt-dlp >/dev/null || fail "yt-dlp is required by upstream script/prepare.sh"
  command -v ffmpeg >/dev/null || fail "ffmpeg is required by upstream script/prepare.sh"
  command -v modelscope >/dev/null || fail "modelscope is required by upstream script/prepare.sh"
  (cd "$bench_root" && bash script/prepare.sh)
fi

printf 'WildClawBench root: %s\n' "$(cd "$bench_root" && pwd)"
printf 'Revision: %s\n' "$(git -C "$bench_root" rev-parse HEAD)"
printf 'Harness image: %s\n' "$image_tag"
