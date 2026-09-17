# QwenClawBench

[Documentation home](../index.md) · [Bench integration and scoring](bench-format.md) · [CLI reference](../start/cli-reference.md)

QwenClawBench v1.1 contains 100 real-user-distribution OpenClaw tasks across
8 domains. Upstream: <https://github.com/SKYLENAGE-AI/QwenClawBench>.

Roller reuses the upstream tasks, assets, automated graders, LLM rubrics,
weights, and 0.75 penalty threshold. It supports OpenClaw, Codex, Claude Code,
Hermes, OpenCode, DeepSeek, and Pi. The adapter defaults to
`harness: openclaw`, which `--harness` can override. Other Harnesses can be
explored, but comparable leaderboard experiments should use OpenClaw and fix
the image, agent model, judge model, and run count.

## 1. Data and export

```bash
git clone https://github.com/SKYLENAGE-AI/QwenClawBench.git \
  /NAS/zqy/QwenClawBench

# The official setup runs each task three times, producing 300 task rows.
roller bench export qwenclawbench /NAS/zqy/QwenClawBench \
  --runs-per-task 3 --output /tmp/qwenclawbench.jsonl
```

`ROOT` may be the upstream repository or the dataset directory that directly
contains `tasks/` and `assets/`. `--category` and `--task-id` are repeatable.
At export time, Roller materializes the upstream
`workspace_files.source/content -> dest` mapping under
`QWENCLAW_CACHE_DIR`, which defaults to
`~/.cache/roller/benches/qwenclawbench`.

Repeated row ids end in `__run_01`, `__run_02`, and `__run_03`. The original
id, run index, and total run count remain in
`bench.metadata.upstream_task_id`, `run_index`, and `runs_per_task`.

## 2. One-command runner

`scripts/run-qwenclawbench.sh` exports tasks, collects trajectories, and writes
`<OUTPUT_ROOT>/<RUN_NAME>/report.json`. Put model and judge credentials in
environment variables before a full hybrid run:

```bash
export LOCAL_VLLM_API_KEY=local
export QWENCLAW_JUDGE_BASE_URL=http://127.0.0.1:8000/v1
export QWENCLAW_JUDGE_API_KEY=local
export QWENCLAW_JUDGE_MODEL=qwen3-30b-a3b-local

QWENCLAW_ROOT=/path/to/QwenClawBench \
MODEL=local-vllm/qwen3-30b-a3b-local \
MODEL_CONFIG=examples/config/openclaw/models-local-vllm.json \
./scripts/run-qwenclawbench.sh
```

The script defaults to OpenClaw and three runs per task. `TASK_IDS` and
`CATEGORIES` accept comma-separated lists. `PARALLEL`, `RETRIES`,
`RUNS_PER_TASK`, `OUTPUT_ROOT`, and `RUN_NAME` override their defaults. Set
`QWENCLAW_REQUIRE_JUDGE=0` only when selecting automated-only tasks. For
another Harness, set `HARNESS`, `API_BASE`, and `API_KEY_ENV`; `API_KEY_ENV`
is the name of the variable holding the model API key:

```bash
export ROLLER_MODEL_API_KEY=replace-me

QWENCLAW_ROOT=/path/to/QwenClawBench \
MODEL=your-model-id \
HARNESS=opencode \
API_BASE=https://api.example.com/v1 \
API_KEY_ENV=ROLLER_MODEL_API_KEY \
TASK_IDS=task_00036_find_largest_file_in_downloads_directory \
RUNS_PER_TASK=1 \
QWENCLAW_REQUIRE_JUDGE=0 \
./scripts/run-qwenclawbench.sh
```

Arguments after the script are appended to `roller collect`, for example
`--resume` or `--thinking high`. `MAX_STEPS` maps to the corresponding Claude
Code, Hermes, OpenCode, DeepSeek, or Pi option. OpenClaw and Codex currently
have no common CLI option for that limit.

## 3. Local Qwen

The repository includes
`examples/config/openclaw/models-local-vllm.json`. It points OpenClaw to
vLLM at `http://127.0.0.1:8000/v1`; `collect` uses host networking by default.

```bash
export LOCAL_VLLM_API_KEY=local

# Start with the only automated-only task. It does not need a judge.
roller bench export qwenclawbench /NAS/zqy/QwenClawBench \
  --task-id task_00036_find_largest_file_in_downloads_directory \
  --output /tmp/qcb-smoke.jsonl

roller collect --tasks /tmp/qcb-smoke.jsonl \
  --model local-vllm/qwen3-30b-a3b-local \
  --harness openclaw \
  --model-config examples/config/openclaw/models-local-vllm.json \
  --task-timeouts --output /tmp/qcb-runs --run-name local-qwen-smoke
```

Confirm that the vLLM model id matches the JSON before running:

```bash
curl http://127.0.0.1:8000/v1/models
```

For external API runs, Codex uses the Responses API, Claude Code uses the
Anthropic Messages API, and Hermes, OpenCode, DeepSeek, and Pi use an
OpenAI-compatible Chat Completions API. See the
[CLI reference](../start/cli-reference.md) for endpoint, key, and Harness step
options.

## 4. Judge API

The other 99 tasks are hybrid and require an OpenAI-compatible
`POST /v1/chat/completions` judge. The grader runs in the container.
`bench.grade.env` injects these variables automatically, so `--pass-env` is
not required:

| Variable | Meaning |
| --- | --- |
| `QWENCLAW_JUDGE_BASE_URL` | API base or full `/chat/completions` URL |
| `QWENCLAW_JUDGE_API_KEY` | Bearer token; use a non-empty placeholder for an unauthenticated local server |
| `QWENCLAW_JUDGE_MODEL` | Judge model; the first `provider/` prefix is removed before the request |
| `QWENCLAW_JUDGE_MAX_RETRIES` | Request attempts; default 3, clamped to 1 through 100 |

`JUDGE_BASE_URL`, `JUDGE_API_KEY`, and `JUDGE_MODEL` are compatibility
fallbacks for the first three variables. The dedicated `QWENCLAW_*` names win.

Use the same local Qwen for task execution and judging:

```bash
export QWENCLAW_JUDGE_BASE_URL=http://127.0.0.1:8000/v1
export QWENCLAW_JUDGE_API_KEY=local
export QWENCLAW_JUDGE_MODEL=qwen3-30b-a3b-local

roller collect --tasks /tmp/qwenclawbench.jsonl \
  --model local-vllm/qwen3-30b-a3b-local \
  --harness openclaw \
  --model-config examples/config/openclaw/models-local-vllm.json \
  --task-timeouts --parallel 1 \
  --output /tmp/qcb-runs --run-name local-qwen
```

To test an external API later, replace only these three judge variables. A
different judge endpoint, model, or prompt changes scores, so record and fix
them for comparative runs.

## 5. Scoring and aggregation

The automated score is the mean of the upstream numeric checks. The LLM judge
returns a rubric score from 0 to 1. Hybrid tasks first compute:

```text
score_simple = (w_auto * s_auto + w_llm * s_llm) / (w_auto + w_llm)
```

The default `overall_score` applies the upstream penalty: when
`s_auto < 0.75`, the LLM contribution becomes zero; otherwise its original
score is used. Roller retains `automated_score`, `llm_judge_score`,
`score_simple`, and `overall_score`.

Before exiting, the grader snapshots the complete final workspace into
`.roller_outputs/`. Roller then collects it into the sample's `results/`.
Scores are in `score.json`; run metadata is in `info.json`.
Before grading, Roller converts each Harness's native journal to the same ACTF
shape and normalizes messages, reasoning, tool arguments, and tool results for
the upstream grader. This does not replace the Harness-native exported trace.

```bash
roller bench report /tmp/qcb-runs/local-qwen
roller bench report /tmp/qcb-runs/local-qwen --output /tmp/qcb-report.json
```

Rollouts for the same upstream task are averaged first. `strict_accuracy`
counts a task only when its mean is 1.0. The report also records the number of
completed `runs_per_task` for every upstream task.

## 6. Comparability boundary

- The official setup uses OpenClaw and three runs per task. Label changes to
  the harness or run count.
- `--task-timeouts` is required to honor the upstream 1800-second task budget.
- The judge is part of the scoring protocol. External API tests must fix the
  base URL, model, and service version.
- Workspace export rejects absolute paths, `..`, symlinks, and colliding
  destinations to prevent asset escape.
- The adapter fixes `overall_score` to penalized scoring. It does not expose
  upstream `--simple-scoring`; `score_simple` is retained for diagnostics.

## 7. Evaluation reports

- [Full evaluation of DeepSeek V4 Flash and GLM-5.2 (2026-09-17)](qwenclawbench-evaluation-2026-09-17.md)
