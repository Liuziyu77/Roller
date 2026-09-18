# Getting started: from install to the first trajectory

[Documentation home](../index.md) · [Examples](examples.md) · [CLI reference](cli-reference.md) · [Trajectory outputs](../traces/trajectory-outputs.md)

This page covers environment setup, API configuration, dry-run, a live collection, result checks, and the Viewer. The finished run includes native raw, ACTF, SFT, and run info.

## 1. Prepare the runtime environment

You need:

- Python 3.10+;
- Docker CLI;
- a Docker daemon the CLI can reach;
- at least one Harness image;
- a model API that matches that Harness protocol.

Install Roller from the repository root:

```bash
python3 -m pip install -e .
roller --version
docker version
```

`docker version` must return both Client and Server information. With only the Docker CLI and no reachable daemon, Roller cannot start a rollout.

## 2. Choose a Harness and image

OpenClaw works with both Responses and Chat Completions, so it is a good first environment check. The dynamic User Simulator can be paired with OpenClaw, Codex, Claude Code, Hermes, OpenCode, DeepSeek, or Pi.

| Harness | API requirements | Default image | How to prepare |
| --- | --- | --- | --- |
| OpenClaw | OpenAI Responses or Chat Completions | `ghcr.io/openclaw/openclaw:2026.7.1-2` | Pull or load directly |
| Codex | Responses streaming | `roller-codex:0.149.0` | Build from the repository Dockerfile |
| Claude Code | Anthropic `/v1/messages` | `roller-claude-code:2.1.222` | Build from the repository Dockerfile |
| Hermes | Chat Completions | `roller-hermes:d15ed4445207` | [Build and configure Hermes](../harnesses/hermes-configuration.md) |
| OpenCode | OpenAI Chat Completions | Native CLI sessions and tools → ACTF/SFT | Pinned version; Task Agent or User Simulator |
| DeepSeek Harness | DeepSeek Chat Completions | Native SDK / persistent Bash → ACTF/SFT | sdk-minimal; Agent / Simulator |
| Pi Agent | Chat Completions | `roller-pi:0.85.1` | [Build and configure](../harnesses/pi-configuration.md) |

Choose CoT settings based on how the model returns reasoning:

- GPT reasoning models (for example `gpt-5.6-sol`): use OpenClaw/Codex `external` or `external_always` to capture explicit CoT;
- Claude models (for example Claude Opus 4.8, sample ID `claude-opus-4-8`): use Claude Code signature recovery;
- Open-source models such as Qwen and GLM: keep the default `--keep-thinking`. Plaintext reasoning returned by the service is written into ACTF/SFT `reasoning_content`; external thinking is not required.

If the service only reports reasoning token counts and no plaintext reasoning, the third option will not produce CoT text.

### OpenClaw

Pull online or load offline:

```bash
docker pull --platform linux/amd64 ghcr.io/openclaw/openclaw:2026.7.1-2

# 或
docker load -i /path/to/openclaw-2026.7.1-2-linux-amd64.tar.gz
```

### Codex

```bash
docker pull --platform linux/amd64 node:22-bookworm-slim
docker build -f docker/codex/Dockerfile -t roller-codex:0.149.0 .
docker run --rm roller-codex:0.149.0 codex --version
```

If you already have a Node 22 offline archive, load it first:

```bash
docker load -i /path/to/node-22-bookworm-slim-linux-amd64.tar.gz
```

Full build flags and how to export the image across machines are in the [Codex Harness guide](../harnesses/codex-configuration.md#build-a-pinned-image).

### Claude Code

Claude Code can reuse the same Node 22 base image as Codex:

```bash
docker build -f docker/claude-code/Dockerfile -t roller-claude-code:2.1.222 .
docker run --rm roller-claude-code:2.1.222 claude --version
```

Full build flags are in the [Claude Code Harness guide](../harnesses/claude-code-configuration.md#build-a-pinned-image).

If you use a custom tag, pass `--docker-image`, or set the matching environment variable among `ROLLER_OPENCLAW_IMAGE`, `ROLLER_CODEX_IMAGE`, and `ROLLER_CLAUDE_CODE_IMAGE`.

## 3. Prepare API configuration

Copy the local environment template:

```bash
cp examples/env/api.env.example examples/env/api.env
```

Then load it into the current shell:

```bash
set -a
source examples/env/api.env
set +a
```

`examples/env/*.env` is Git-ignored, but still never put a real key in shell history, a task, a model JSON file, or documentation.

### OpenClaw configuration

OpenClaw reads the provider and model from `--model-config`:

```json
{
  "mode": "merge",
  "providers": {
    "my-openai-proxy": {
      "baseUrl": "${MY_PROXY_BASE_URL}",
      "apiKey": "${MY_PROXY_API_KEY}",
      "api": "openai-responses",
      "models": [{"id": "my-model", "name": "My model"}]
    }
  }
}
```

- `mode: "merge"` means the providers declared here are merged into OpenClaw's existing model config, instead of replacing the whole config set;
- `--model` uses `provider/model-id`; the example above is `my-openai-proxy/my-model`;
- `${ENV_NAME}` is expanded by Roller on the host; a missing or empty value fails before the rollout starts;
- Responses services use `openai-responses`; services that only expose `/v1/chat/completions` use `openai-completions`;
- `baseUrl` usually ends at `/v1`; do not treat `/chat/completions` as the base URL.

The repository template is [`examples/config/openclaw/models.json`](../../../examples/config/openclaw/models.json). Reasoning, multimodal, and native Harness config templates are in [Examples](examples.md#harness-configuration-templates).

### Codex configuration

Codex does not read OpenClaw model JSON. It uses:

```bash
--api-base "$ROLLER_CODEX_BASE_URL" \
--api-key-env ROLLER_CODEX_API_KEY
```

The API must support Responses streaming, reasoning, and tool call/result. A working Chat Completions endpoint does not mean Codex can use that service.

### Claude Code configuration

Claude Code also does not read OpenClaw model JSON. It uses:

```bash
--api-base "$ROLLER_ANTHROPIC_BASE_URL" \
--api-key-env ROLLER_ANTHROPIC_API_KEY
```

`--api-base` can be an origin, a base ending in `/v1`, or a full `/v1/messages` endpoint; Roller normalizes it before handing it to Claude Code.

## 4. Meet the example Task

[`examples/tasks/tasks.jsonl`](../../../examples/tasks/tasks.jsonl) contains four tasks from simple to more involved:

| ID | What it shows |
| --- | --- |
| `01_text_only` | Single-turn plain text |
| `02_local_markdown` | Read an input file and write to `outputs/` |
| `03_user_memory_context` | Inject `USER.md` and `MEMORY.md` |
| `04_multi_turn` | Two predefined user messages share one session |

A minimal task has only three fields:

```json
{"schema_version":"roller.task/v1","id":"01_text_only","prompt":"计算 2+3，只回复数字。"}
```

`--model` can be set once on the command line; if omitted, each task must have its own `model` field. Full schema: [Task format](../tasks/task-format.md).

## 5. Dry-run

Using OpenClaw as the example:

```bash
roller collect \
  --tasks examples/tasks/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/config/openclaw/models.json \
  --parallel 4 \
  --dry-run
```

A dry-run will:

- read and normalize JSON/JSONL tasks;
- check duplicate IDs, conversation modes, and Harness limits;
- resolve input, Context, and output paths;
- run path-conflict, symlink, file-size, and content-digest checks;
- print the expanded task × model rollout plan.

It does not check the Docker daemon, whether images exist, or whether the model API is reachable, and it does not create a run directory.

## 6. Collect for real

```bash
roller collect \
  --tasks examples/tasks/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/config/openclaw/models.json \
  --parallel 4 \
  --retries 1 \
  --timeout 600 \
  --output runs \
  --run-name quickstart
```

What the flags mean:

| Flag | Meaning |
| --- | --- |
| `--parallel 4` | Run at most 4 rollouts at once |
| `--retries 1` | After the first failure, retry at most 1 more time |
| `--timeout 600` | Total timeout per rollout is 600 seconds |
| `--output runs` | Host root directory for all runs |
| `--run-name quickstart` | This run is saved as `runs/quickstart` |

If a directory with the same name already exists, Roller refuses to overwrite it by default. Continue the original run with `--resume`; start a new experiment with a new `--run-name`.

## 7. Check the results

After success, inspect the run root first:

```text
runs/quickstart/
├── info.json
├── lock.json
├── sft.jsonl
└── samples/
    └── <model>/
        └── <index>_<task-id>/
```

Each successful sample should at least have:

```text
trace.jsonl
trace_ACTF.json
trace_SFT.json
info.json
```

Check in this order:

1. Whether run `info.json.status` is `completed`, and whether success/failure counts match expectations;
2. Whether sample `info.json.status` is `ok`, and whether `usage` and version fields exist;
3. Whether reasoning, tool calls, and results are paired correctly in `trace_ACTF.json`;
4. Whether the top level of `trace_SFT.json` contains only `messages` and
   `tools` (image-bearing samples additionally carry `images`, with one
   `<image>` placeholder per entry);
5. For tasks that must deliver files, whether the sample `results/` contains the expected files;
6. Whether the number of lines in run `sft.jsonl` equals the number of successful samples that can be used for training.

Generation conditions and failure semantics for each file are in [Trajectory outputs](../traces/trajectory-outputs.md).

## 8. Open the Viewer

```bash
roller view runs/quickstart
```

It listens on `127.0.0.1:8765` by default. When running remotely, keep loopback and set up SSH port forwarding from your machine:

```bash
ssh -L 8765:127.0.0.1:8765 your-host
```

The Viewer shows the ACTF timeline; Inspector is for the system prompt, tools schema, run info, and raw content. See the [Viewer guide](../traces/viewer.md).

## 9. Switch to Codex or Claude Code

### Codex

```bash
roller collect \
  --harness codex \
  --tasks examples/tasks/tasks.jsonl \
  --model gpt-5.6-sol \
  --api-base "$ROLLER_CODEX_BASE_URL" \
  --api-key-env ROLLER_CODEX_API_KEY \
  --thinking high \
  --reasoning-summary detailed \
  --parallel 2 \
  --timeout 900 \
  --output runs \
  --run-name codex-quickstart
```

### Claude Code

```bash
roller collect \
  --harness claude-code \
  --tasks examples/tasks/tasks.jsonl \
  --model claude-sonnet-4-6-thinking \
  --api-base "$ROLLER_ANTHROPIC_BASE_URL" \
  --api-key-env ROLLER_ANTHROPIC_API_KEY \
  --thinking high \
  --parallel 2 \
  --timeout 900 \
  --output runs \
  --run-name claude-code-quickstart
```

Do not pass OpenClaw `--model-config` to a run that has no OpenClaw participant. Codex and Claude Code also support `prompt`, predefined `messages`, and dynamic `user_simulator`; the Simulator Harness/model can be set independently in the task's `user_simulator` object.

## 10. Next steps

- Copy more commands: [Examples](examples.md)
- Write your own data: [Task format](../tasks/task-format.md)
- Configure reasoning: [OpenClaw](../harnesses/openclaw-configuration.md#thinking-system-prompt-and-wire-trace) · [Codex](../harnesses/codex-configuration.md#reasoning-configuration) · [Claude Code](../harnesses/claude-code-configuration.md#thinking-and-effort)
- Collect explicit CoT: [External Thinking](../traces/external-thinking.md)
- Understand the training format: [SFT data format](../traces/sft-format.md)
- See every CLI option: [CLI reference](cli-reference.md)

## Common first-run issues

| Symptom | What to check |
| --- | --- |
| Docker preflight failed | Whether `docker version` can reach the Server; whether the image has been pulled/loaded |
| OpenClaw cannot find the model | Whether `--model` matches `provider/models[].id` in the JSON exactly |
| Config reports a missing environment variable | Whether you `source examples/env/api.env`; whether placeholder names match |
| `/responses` returns 404 | The service only supports Chat Completions; switch OpenClaw to `openai-completions`, or change the Codex API |
| Claude URL repeats `/v1` | Always pass origin, `/v1`, or `/v1/messages` through `--api-base` |
| A run with the same name already exists | Use `--resume` for the original experiment; change `--run-name` for a new one |
| Viewer does not show new tasks | Confirm you passed the run root directory, refresh the page, and check run `info.json` |

More specific errors are listed at the end of each of the six Harness guides.
