# PinchBench

[Documentation](../index.md) · [Bench integration and scoring](bench-format.md) · [OpenClaw configuration](../harnesses/openclaw-configuration.md)

Roller exports PinchBench from its canonical `tasks/manifest.yaml` and supports automated,
LLM-judge, hybrid, and true multi-session tasks. Upstream: <https://github.com/pinchbench/skill>.

## Shortest path

```bash
scripts/prepare-pinchbench.sh "$PWD/.benchmarks/pinchbench"
export PINCHBENCH_ROOT="$PWD/.benchmarks/pinchbench"

set -a
. /path/to/credentials.env
set +a
export ROLLER_RUN_ROOT=$PWD/runs/pinchbench
mkdir -p "$ROLLER_RUN_ROOT"
```

The preparation script clones the upstream data and pulls the pinned `linux/amd64` OpenClaw image.
It is idempotent: an existing checkout is never pulled or rewritten, and no API key is read or
written. The script prints the effective commit so formal comparisons can record it. The equivalent
manual procedure is to clone <https://github.com/pinchbench/skill> and pull
`ghcr.io/openclaw/openclaw:2026.7.1-2`.

Create an OpenClaw model config with no literal secret, such as `example.models.json`. The
compatibility model ID below is backed by Example model:

```json
{
  "mode": "merge",
  "providers": {
    "example": {
      "baseUrl": "https://api.example.com/v1",
      "apiKey": "${MODEL_API_KEY}",
      "api": "openai-completions",
      "timeoutSeconds": 600,
      "models": [{"id": "example-model", "name": "Example model"}]
    }
  }
}
```

First run an automated-only smoke test that needs no judge:

```bash
roller collect --bench pinchbench --bench-root "$PINCHBENCH_ROOT" \
  --bench-variant automated-only \
  --bench-task-id task_sanity --bench-task-id task_files \
  --model example/example-model --model-config example.models.json \
  --parallel 1 --grader-parallel 1 \
  --output "$ROLLER_RUN_ROOT" --run-name smoke
```

This command exports, runs with per-task timeouts, grades, and writes both
`smoke/tasks.jsonl` and `smoke/bench-report.json` automatically.

For comparable LLM/hybrid runs in `all` or `core`, use PinchBench's current
default judge, `anthropic/claude-haiku-4.5`. Only an OpenRouter key is required;
Roller defaults to `https://openrouter.ai/api/v1`:

```bash
export OPENROUTER_API_KEY=...

roller collect --bench pinchbench --bench-root "$PINCHBENCH_ROOT" \
  --bench-variant core --bench-timeout-multiplier 2 \
  --model example/example-model --model-config example.models.json \
  --samples 1 --parallel 1 --grader-parallel 1 \
  --output "$ROLLER_RUN_ROOT" --run-name core
```

## Variants and execution semantics

| Variant | Contents |
| --- | --- |
| `all` | Every task in the manifest; the default |
| `automated-only` | Pure automated graders only; useful for inexpensive smoke tests |
| `core` | The representative core set declared by the manifest |

- `workspace_files` are content-addressed first, then copied into a writable workspace for each sample; the source checkout is never modified.
- `sessions[].new_session: true` starts a new OpenClaw session while preserving the container and filesystem; grading combines all session transcripts.
- Private image answers enter the container only after the Agent has finished.
- The default judge model, prompt, system message, and direct-API request parameters match current upstream PinchBench: `anthropic/claude-haiku-4.5`, `temperature=0`, `max_completion_tokens=2048`, non-streaming, a 300-second timeout, and one request. The overall grader wall-clock cap is 3600 seconds; it only prevents the surrounding grader process from hanging and does not change the judge request.
- Override the judge with `PINCHBENCH_JUDGE_BASE_URL`, `PINCHBENCH_JUDGE_API_KEY`, and `PINCHBENCH_JUDGE_MODEL` for a custom OpenAI-compatible gateway. Set `PINCHBENCH_JUDGE_MAX_TOKENS_FIELD=max_tokens` only when a compatibility endpoint requires it, and set `PINCHBENCH_JUDGE_STREAM=true` only when streaming is required. Such runs validate compatibility; scores are not official-comparable when the judge model differs.
- Reasoning-model compatibility judges may consume the 2048-token official default before emitting their JSON. Increase `PINCHBENCH_JUDGE_MAX_TOKENS` (for example, to `8192`) for those models. When `PINCHBENCH_JUDGE_RETRIES` is nonzero, Roller retries transport failures as well as successful responses that contain no parseable score; streamed `reasoning_content` is also accepted as a fallback when `content` is empty.
- Frontmatter `prerequisites` are retained in bench metadata. For the four FWS/GWS/GitHub integration tasks, Roller automatically installs pinned compatible `fws`, `gws`, and `gh` versions in the disposable task container, starts the upstream FWS mock service, and exposes the CLIs to OpenClaw. The downloads honor the shell's standard HTTP(S) proxy variables.

## Upstream flag mapping

| PinchBench | Roller |
| --- | --- |
| `--model`, `--base-url`, `--thinking` | `collect --model`, model config/`--api-base`, `--thinking` |
| `--judge` | Defaults to the official judge; override with `PINCHBENCH_JUDGE_MODEL` plus judge base URL/key environment variables |
| `--suite all/automated-only`, task IDs, core/category | `collect --bench-variant`, `--bench-task-id`, `--bench-category` |
| `--runs N` | `collect --samples N` |
| `--timeout-multiplier N` | `collect --bench-timeout-multiplier N` |
| parallel judge toggle | `collect --grader-parallel N`; use 1 for serial grading |
| `--no-fail-fast` | Roller already records failures and continues through the run |
| upload/register/trend/judge cache | Post-processing/leaderboard features; Roller reports locally and never auto-uploads |

Roller's local report is not uploaded to the PinchBench leaderboard. Tasks that depend on search,
email, calendar, third-party integrations, or a skill registry may still require corresponding
network access and credentials. The four upstream FWS mock tasks are provisioned automatically.
