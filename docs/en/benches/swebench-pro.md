# SWE-Bench Pro

[Documentation home](../index.md) · [Bench integration and scoring](bench-format.md) · [CLI reference](../start/cli-reference.md)

Evaluation report:
[DeepSeek V4 Pro fixed-first-100 results (2026-09-19)](swebench-pro-evaluation-2026-09-19.md).

Roller supports all 731 tasks and 11 repositories in the public SWE-Bench Pro
test split. It reuses the official Docker images, `run_script.sh`, `parser.py`,
F2P/P2P sets, and resolved criterion.
Upstream: <https://github.com/scaleapi/SWE-bench_Pro-os>.

## 1. Data and export

The prepared local dataset root is `/NAS/zqy/SWE-bench_Pro`. It contains
`test.jsonl`, `test.parquet`, `test_patches/`, and the official `run_scripts/`.

```bash
roller bench export swebench-pro /NAS/zqy/SWE-bench_Pro \
  --output /tmp/swebench-pro.jsonl

roller bench export swebench-pro /NAS/zqy/SWE-bench_Pro \
  --task-id instance_flipt-io__flipt-a0cbc0cb65ae601270bdbe3f5313e2dfd49c80e4 \
  --output /tmp/swebench-pro-smoke.jsonl
```

`--category` takes a repository name such as `flipt-io/flipt`. The adapter
prefers `test.jsonl`. If the root only has Parquet, run
`pip install -e '.[swebench-pro]'`.

## 2. One-command runner

`scripts/run-swebench-pro.sh` exports tasks, collects trajectories, and writes
`<OUTPUT_ROOT>/<RUN_NAME>/report.json`. Start with one instance as a smoke
test:

```bash
export MY_PROXY_BASE_URL=https://api.example.com/v1
export MY_PROXY_API_KEY=replace-me

SWEBENCH_PRO_ROOT=/path/to/SWE-bench_Pro \
MODEL=my-openai-proxy/my-model \
MODEL_CONFIG=examples/config/openclaw/models.json \
TASK_IDS=instance_flipt-io__flipt-a0cbc0cb65ae601270bdbe3f5313e2dfd49c80e4 \
./scripts/run-swebench-pro.sh
```

`HARNESS` defaults to `openclaw`. `TASK_IDS` and `CATEGORIES` accept
comma-separated lists. `PARALLEL`, `RETRIES`, `OUTPUT_ROOT`, and `RUN_NAME`
override their defaults. For another Harness, set `HARNESS`, `API_BASE`, and
`API_KEY_ENV`; `API_KEY_ENV` is the name of the variable that holds the key,
not the key itself. Optional `MAX_STEPS` maps to the corresponding Claude Code,
Hermes, OpenCode, DeepSeek, or Pi option. Arguments after the script are
appended to `roller collect`, for example `--resume`.

## 3. Harnesses and model APIs

```bash
export MY_PROXY_BASE_URL=https://api.example.com/v1
export MY_PROXY_API_KEY=replace-me

roller collect --tasks /tmp/swebench-pro-smoke.jsonl \
  --model my-openai-proxy/my-model \
  --harness openclaw \
  --model-config examples/config/openclaw/models.json \
  --task-timeouts --parallel 1 \
  --output /tmp/swebench-pro-runs --run-name local-qwen-smoke
```

Roller supports OpenClaw, Codex, Claude Code, Hermes, OpenCode, DeepSeek, and
Pi. Exported tasks default to `harness: openclaw`; `--harness` overrides it.
Codex uses the Responses API, Claude Code uses the Anthropic Messages API, and
the other non-OpenClaw harnesses use an OpenAI-compatible Chat Completions API.
See the [CLI reference](../start/cli-reference.md) for endpoint and key options.

On the first run of an instance, Roller pulls
`jefzda/sweap-images:<dockerhub_tag>` and combines it with the selected Harness
image. The OpenClaw runtime is installed into the official instance image.
Other Harnesses use their own runtime image and copy the official `/app`
repository into it. Derived images are cached by source image, Harness image,
and Harness name. The first run needs substantial network bandwidth, disk, and
startup time; later runs reuse the local image.

## 4. Isolation and scoring

The Agent works in `/app` in the derived image. At startup, Roller resets to
`base_commit`, records the original tracked files, removes the upstream `.git`
history, and creates a fresh repository containing only the baseline snapshot.
The Agent therefore cannot recover the gold commit from Git, and exported tasks
do not contain the gold source patch. For non-OpenClaw Harnesses, system
dependencies come from the Harness image and may differ from the official
instance image.

After the Agent's last turn, Roller extracts tracked, untracked, and committed
model changes into `model.patch`, removes binary hunks as upstream does, and
starts a fresh official instance container. Only this independent container
receives the model patch, hidden `test.patch`, official runner, and parser. The
grader applies the model patch to a clean baseline and then overwrites official
test files with their hidden versions. The independent grader container is
removed after success or failure. A task is resolved only when the union of F2P
and P2P tests is in the parser's `PASSED` set. `overall_score` is 1 for resolved
and 0 otherwise.

Each sample's `results/` contains `model.patch`, `test_stdout.log`,
`test_stderr.log`, and `test_output.json`. The score remains in `score.json`.

```bash
roller bench report /tmp/swebench-pro-runs/local-qwen-smoke
```

## 5. Comparability boundary

- Always score in a fresh instance container. Do not reuse a manually modified
  container.
- Use `--task-timeouts`. The Agent budget is 3600 seconds per task; grading has
  a separate 3600-second limit.
- This integration uses local Docker and does not implement upstream Modal.
- All seven Harnesses use the same fresh-official-image scoring path. Record
  Agent environment differences in experiment reports.
- Container networking remains enabled for the model API; this is not
  equivalent to upstream `--block_network`.
- A full run can pull many per-instance images. Start with `--task-id` and
  monitor Docker disk usage.

## 6. Optional environment and grading corrections

OpenClaw uses `/opt/roller-node/bin/node`, preserving the project's original Node.
The Python `SWEBenchProAdapter` API offers three options, disabled by default and recorded in exported manifests:

- `go_proxy="https://goproxy.cn,direct"`: preserve the image's Go cache paths and make Go available to login shells. If tests report missing go.sum entries, download dependencies and rerun within the original grading time budget. Save dependency edits and the first failed logs separately.
- `repair_test_fixtures=True`: extract only non-executable text data from test-data directories in the upstream gold patch to supply missing fixtures during grading. These files never enter the Agent workspace or prompt.
- `exclude_agent_tests=True`: exclude model changes to Go/Python/JS test source during grading to avoid collisions with hidden tests. Preserve `model.patch`; write the actual grading patch to `graded.patch`.

These options preserve the official pass criterion while changing runtime and
grading infrastructure. Report both original and corrected scores. Cumulative
recovery from rerunning failures is not an independent pass@1 measurement.
