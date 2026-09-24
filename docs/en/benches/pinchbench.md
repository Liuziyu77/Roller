# PinchBench

[Documentation](../index.md) · [Benchmark run standard](benchmark-standard.md) · [Bench integration and scoring](bench-format.md) · [OpenClaw configuration](../harnesses/openclaw-configuration.md)

Roller reads PinchBench tasks from `tasks/manifest.yaml`. It supports automated grading, LLM
judging, hybrid grading, and multiple sessions. The upstream repository is
<https://github.com/pinchbench/skill>.

## Prepare the environment

```bash
export PINCHBENCH_ROOT="$PWD/.benchmarks/pinchbench"
scripts/prepare-benchmark.sh pinchbench "$PINCHBENCH_ROOT"

export ROLLER_MODEL_ID=example/model
export ROLLER_MODEL_BASE_URL=https://api.example.com/v1
export ROLLER_MODEL_API_KEY=replace-with-your-api-key
export ROLLER_BENCH_OUTPUT="$PWD/runs/pinchbench"
```

`prepare-pinchbench.sh` clones the upstream data when the target directory is missing and pulls
the pinned `linux/amd64` OpenClaw image. It does not update an existing checkout. The script
prints the commit for experiment records and does not read or write API keys. To prepare the
environment by hand, clone <https://github.com/pinchbench/skill> and pull
`ghcr.io/openclaw/openclaw:2026.7.1-2`.

The preparation script pins revision `819384ae830492365b8363fc26bc2602e73f216d`. It stops without
changing the checkout when an existing directory is at another revision.

Run two automatically graded tasks before starting a larger job. They do not need a judge:

```bash
scripts/run-benchmark.sh --config examples/benchmarks/base.yaml \
  --extra-config examples/benchmarks/pinchbench.yaml \
  --extra-config '{"benchmark":{"variant":"automated-only","task_ids":["task_sanity","task_files"]},"execution":{"parallel":1,"grader_parallel":1,"run_name":"pinchbench-smoke"}}' \
  --dry-run

scripts/run-benchmark.sh --config examples/benchmarks/base.yaml \
  --extra-config examples/benchmarks/pinchbench.yaml \
  --extra-config '{"benchmark":{"variant":"automated-only","task_ids":["task_sanity","task_files"]},"execution":{"parallel":1,"grader_parallel":1,"run_name":"pinchbench-smoke"}}'
```

The command writes `tasks.jsonl` and `bench-report.json` under
`$ROLLER_BENCH_OUTPUT/pinchbench-smoke/`.

The `all` and `core` variants contain LLM and hybrid grading tasks. The checked-in configuration
maps three shared judge variables to PinchBench's environment names. Set them before the dry run:

```bash
export ROLLER_JUDGE_BASE_URL=https://judge.example.com/v1
export ROLLER_JUDGE_API_KEY=replace-with-your-judge-key
export ROLLER_JUDGE_MODEL=example-judge

scripts/run-benchmark.sh --config examples/benchmarks/base.yaml \
  --extra-config examples/benchmarks/pinchbench.yaml --dry-run
scripts/run-benchmark.sh --config examples/benchmarks/base.yaml \
  --extra-config examples/benchmarks/pinchbench.yaml
```

Roller generates the OpenClaw models file from the common model fields; no separate models JSON is needed.

## Variants

| Variant | Contents |
| --- | --- |
| `all` | Every task in the manifest; this is the default |
| `automated-only` | Tasks with automated graders, useful for checking the environment |
| `core` | The core subset declared by the manifest |

## Execution and scoring

Roller takes a content-addressed snapshot of `workspace_files` and copies it into a writable
workspace for each sample. It never changes the upstream checkout.
`sessions[].new_session: true` starts another OpenClaw session in the same container, preserving
the filesystem. The grader receives the combined transcript. Private answers for the image task
enter the container after the agent exits.

The default judge request matches the current upstream PinchBench settings:
`anthropic/claude-haiku-4.5`, `temperature=0`,
`max_completion_tokens=2048`, no streaming, a 300-second timeout, and one request. A separate
3600-second wall-clock limit stops a stuck grader process without changing that request.

For a custom OpenAI-compatible judge, set `PINCHBENCH_JUDGE_BASE_URL`,
`PINCHBENCH_JUDGE_API_KEY`, and `PINCHBENCH_JUDGE_MODEL`. Set
`PINCHBENCH_JUDGE_MAX_TOKENS_FIELD=max_tokens` or
`PINCHBENCH_JUDGE_STREAM=true` only when the endpoint requires it. Scores from a different judge
model are not directly comparable to the official result.

A reasoning model may use the default 2048-token budget before it emits JSON. Increase the budget
with `PINCHBENCH_JUDGE_MAX_TOKENS=8192`. When `PINCHBENCH_JUDGE_RETRIES` is greater than zero,
Roller retries network errors and successful responses without a parseable score. If streamed
`content` is empty, the grader also checks `reasoning_content`.
If every judge attempt fails, the sample is marked ineligible instead of being counted as a
genuine zero score.

Roller keeps frontmatter `prerequisites` in bench metadata. For the four FWS/GWS/GitHub
integration tasks, it installs pinned versions of `fws`, `gws`, and `gh` in the disposable
task container and starts the upstream FWS mock service. These downloads use the shell's HTTP(S)
proxy settings.

## Upstream option mapping

| PinchBench | Roller |
| --- | --- |
| `--model`, `--base-url`, `--thinking` | `collect --model`, model config/`--api-base`, `--thinking` |
| `--judge` | Uses the official judge by default; override it with the judge model, base URL, and key environment variables |
| `--suite all/automated-only`, task IDs, core/category | `collect --bench-variant`, `--bench-task-id`, `--bench-category` |
| `--runs N` | `collect --samples N` |
| `--timeout-multiplier N` | `collect --bench-timeout-multiplier N` |
| parallel judge toggle | `collect --grader-parallel N`; use 1 for serial grading |
| `--no-fail-fast` | Roller records failures and continues the run |
| upload/register/trend/judge cache | Roller does not implement these leaderboard and post-processing features |

Roller writes reports locally and does not upload them to the PinchBench leaderboard. Tasks that
use search, email, calendar, third-party integrations, or a skill registry still need the matching
network access and credentials. Roller prepares the four upstream FWS mock tasks automatically.
