# Benchmark run standard

Roller provides one configuration entry point and a fixed integration layout for benchmarks. Model connection, Harness, and execution settings live in a base configuration. Benchmark task selection and grading settings live in an extra configuration.

```bash
scripts/run-benchmark.sh \
  --config examples/benchmarks/base.yaml \
  --extra-config examples/benchmarks/wildclawbench.yaml \
  --dry-run
```

Remove `--dry-run` after checking the plan. The command still uses `roller collect --bench` underneath, so existing commands and result formats remain compatible.

## Configuration layers

`--config` accepts YAML or JSON. Repeat `--extra-config` with a file or an inline JSON object. Later layers override earlier ones. Objects merge recursively; lists and scalar values are replaced. Set an optional object to `null` when the next layer needs to discard it, for example when changing Harnesses and clearing the previous `harness.extra` block.

The machine-readable contract is [`schemas/bench-run.schema.json`](../../../schemas/bench-run.schema.json). It describes one layer, so fields may be omitted and supplied by a later layer. Roller validates the merged configuration before loading any containers.

```yaml
schema_version: roller.bench-run/v1

benchmark:
  id: wildclawbench
  root: ${WILDCLAWBENCH_ROOT}
  variant: null
  categories: []
  task_ids: []
  timeout_multiplier: 1
  environment:
    JUDGE_MODEL: ${ROLLER_JUDGE_MODEL}

model:
  id: ${ROLLER_MODEL_ID}
  base_url: ${ROLLER_MODEL_BASE_URL}
  api_key_env: ROLLER_MODEL_API_KEY
  api_protocol: openai-completions  # optional; inferred from the Harness when omitted
  thinking: high

harness:
  id: openclaw
  image: wildclawbench-ubuntu:v1.3
  extra:
    model_request_timeout: 600
    max_tokens: 32768

execution:
  output: ${ROLLER_BENCH_OUTPUT}
  run_name: wildclawbench
  parallel: 4
  grader_parallel: 2
  samples: 1
  retries: 0
  timeout: 600
  wire_trace: actf
  keep_thinking: true
  pass_env: []
```

An environment reference must occupy the whole field, as in `${ROLLER_MODEL_BASE_URL}`. Roller rejects forms such as `https://${HOST}/v1`, which prevents unresolved variables and accidental credential concatenation. `model.api_key_env` names the host variable that contains the model key; the key itself stays out of the file.

Keep credentials out of `model.base_url`. Roller rejects URL user information, query strings, and fragments because the endpoint is passed to the Harness as an argument.

## Common fields

| Field | Purpose |
| --- | --- |
| `benchmark.id` | Adapter registered by `roller bench list` |
| `benchmark.root` | Prepared upstream checkout or data directory |
| `benchmark.variant` | Evaluation condition, such as SkillsBench `with-skill` |
| `benchmark.categories` / `task_ids` | Optional filters; an empty list applies no filter |
| `benchmark.timeout_multiplier` | Scales both the task declaration and execution deadline |
| `benchmark.environment` | Benchmark variables scoped to this process and restored afterward |
| `model.id` | Model under test |
| `model.base_url` | Model API endpoint |
| `model.api_key_env` | Host environment variable containing the model key |
| `model.api_protocol` | API protocol. OpenClaw writes it into the generated provider; other Harnesses validate it against their native protocol |
| `model.thinking` | Common thinking or reasoning-effort level, validated and mapped by each Harness |
| `harness.id` / `image` | Harness and container image |
| `harness.extra` | Harness-specific settings; unknown fields fail before execution |
| `execution.*` | Output, concurrency, attempts, retries, trace, and common container settings |

OpenClaw uses the same `model.id`, `base_url`, `api_key_env`, and `api_protocol`. Roller creates a temporary models file and removes it after the command. OpenClaw model IDs must use `provider/model`. Advanced users can set `harness.extra.model_config` instead. Set inherited `model.base_url`, `model.api_key_env`, and `model.api_protocol` fields to `null` in that layer because a custom models file cannot be combined with them.

When switching away from the Harness selected by a benchmark example, override its image and clear its options together:

```yaml
harness:
  id: codex
  image: wildclawbench-codex-ubuntu:v0.0
  extra: null
```

Codex uses `openai-responses`, Claude Code uses `anthropic-messages`, and Hermes, OpenCode, DeepSeek, and Pi use `openai-completions`. Roller rejects a conflicting `api_protocol` instead of silently ignoring it.

Use `benchmark.environment` for judge and benchmark tool settings. For example, PinchBench can map shared judge variables to its upstream names:

```yaml
benchmark:
  environment:
    PINCHBENCH_JUDGE_BASE_URL: ${ROLLER_JUDGE_BASE_URL}
    PINCHBENCH_JUDGE_API_KEY: ${ROLLER_JUDGE_API_KEY}
    PINCHBENCH_JUDGE_MODEL: ${ROLLER_JUDGE_MODEL}
```

These values are not placed on the command line. Dry-run output and run metadata record the effective non-secret settings, configuration paths, a digest of the redacted configuration, and environment variable names. Secret values are not recorded.

## Harness-specific settings

`harness.extra` uses names without a Harness prefix. Roller maps them to the existing `collect` options.

| Harness | Supported `extra` fields |
| --- | --- |
| OpenClaw | `model_config`, `gateway_port`, `gateway_port_step`, `gateway_startup_timeout`, `model_request_timeout`, `max_tokens`, `session_id`, `search_provider`, `collect_paths`, `set_image_model`, `cache_trace` |
| Codex | `config`, `reasoning_summary`, `web_search`, `websockets` |
| Claude Code | `settings`, `max_turns`, `max_thinking_tokens`, `permission_mode` |
| Hermes | `max_iterations`, `context_length`, `vision` |
| OpenCode | `max_steps`, `context_length`, `output_tokens`, `vision` |
| DeepSeek / Pi | `max_steps`, `context_length`, `output_tokens` |

`model_request_timeout` requires an OpenClaw release whose provider schema accepts `timeoutSeconds`; the older WildClawBench image does not, so its example leaves this field unset.

## The three-stage workflow

### 1. Prepare

Every maintained adapter provides `scripts/prepare-<name>.sh [ROOT]`. Use the dispatcher when scripting several benchmarks:

```bash
scripts/prepare-benchmark.sh wildclawbench "$WILDCLAWBENCH_ROOT"
scripts/prepare-benchmark.sh pinchbench "$PINCHBENCH_ROOT"
scripts/prepare-benchmark.sh skillsbench "$SKILLSBENCH_ROOT"
```

Preparation scripts fetch the expected data and image, check an existing checkout, and print its resolved revision. They do not read or write model or judge keys.

### 2. Run and grade

```bash
export ROLLER_MODEL_ID=example/model
export ROLLER_MODEL_BASE_URL=https://api.example.com/v1
export ROLLER_MODEL_API_KEY=replace-with-your-api-key
export ROLLER_BENCH_OUTPUT="$PWD/runs"

scripts/run-benchmark.sh \
  --config examples/benchmarks/base.yaml \
  --extra-config examples/benchmarks/wildclawbench.yaml \
  --dry-run

scripts/run-benchmark.sh \
  --config examples/benchmarks/base.yaml \
  --extra-config examples/benchmarks/wildclawbench.yaml
```

`--dry-run` loads the adapter, filters tasks, and validates the endpoint, key variable, thinking level, and Harness-specific fields. It prints the effective configuration, task and trajectory counts, image, and per-task timeout without starting containers or creating a run directory.

Roller grades each task before its agent container is removed. A completed run contains `tasks.jsonl`, trajectories, `score.json`, `info.json`, and `bench-report.json`.

### 3. Rebuild a report

The run command creates the report automatically. Rebuild it after auditing or repairing samples with:

```bash
roller bench report runs/<run-name> --output runs/<run-name>/bench-report.json
```

## Reference configurations

| Benchmark | Extra configuration | Preparation script |
| --- | --- | --- |
| WildClawBench | `examples/benchmarks/wildclawbench.yaml` | `scripts/prepare-wildclawbench.sh` |
| PinchBench | `examples/benchmarks/pinchbench.yaml` | `scripts/prepare-pinchbench.sh` |
| SkillsBench with skill | `examples/benchmarks/skillsbench.yaml` | `scripts/prepare-skillsbench.sh` |
| SkillsBench without skill | Add `examples/benchmarks/skillsbench-no-skill.yaml` after the previous layer | Same as above |

WildClawBench is the full reference integration because it exercises an independent judge, search configuration, task-level timeouts, several Harnesses, and delayed ground-truth staging. PinchBench and SkillsBench use the same entry point with smaller overlays.

## Requirements for a new benchmark

A new benchmark should include:

1. A `BenchAdapter` implementation and registry entry.
2. An idempotent `scripts/prepare-<name>.sh` that does not silently switch an existing checkout to another revision.
3. `examples/benchmarks/<name>.yaml` containing only public defaults and environment references.
4. English and Chinese pages organized as preparation, configuration, dry run, execution, scoring, and limitations.
5. Adapter unit tests, a configuration dry-run test, and at least one container smoke test that does not depend on an LLM judge.
6. A parameter mapping table that names unsupported upstream options.

The adapter must stage ground truth through `bench.grade.stage` after the agent exits. Grader failures and valid zero scores must remain distinct. For an LLM judge, record the judge model, endpoint source, and request parameters without persisting its key.
