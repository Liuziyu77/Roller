# Examples

[Documentation home](../index.md) · [Getting started](getting-started.md) · [Task format](../tasks/task-format.md) · [Trajectory outputs](../traces/trajectory-outputs.md)

The repository [`examples/`](https://github.com/Liuziyu77/Roller/tree/main/examples) directory contains config templates, tasks, and run commands. Install steps, field definitions, and trajectory semantics live in the matching topic docs.

## Directory contents

```text
examples/
├── env/
│   └── api.env.example
├── config/
│   ├── openclaw/
│   │   ├── models.json
│   │   ├── models-reasoning.json
│   │   └── models-multimodal.json
│   ├── codex/
│   │   └── config.toml.example
│   └── claude-code/
│       └── settings.json.example
└── tasks/
    ├── tasks.jsonl
    ├── user-simulator.jsonl
    └── files/
        ├── 02_local_markdown/
        │   └── inputs/product-brief.md
        └── 03_user_memory_context/
            └── context/
                ├── USER.md
                └── MEMORY.md
```

## One-time setup

First follow [Getting started](getting-started.md#1-prepare-the-runtime-environment) to install Roller and prepare at least one Harness image.

Copy and edit the local API environment file:

```bash
cp examples/env/api.env.example examples/env/api.env

set -a
source examples/env/api.env
set +a
```

`examples/env/api.env` is Git-ignored. Do not put a real key in JSON, a task, or the command line.

## Harness configuration templates

| Harness | File | Purpose |
| --- | --- | --- |
| OpenClaw | `config/openclaw/models.json` | Minimal text model; model ref is `my-openai-proxy/my-model` |
| OpenClaw | `config/openclaw/models-reasoning.json` | GPT Responses and Chat Completions reasoning models |
| OpenClaw | `config/openclaw/models-multimodal.json` | Declares `text` + `image` input capability |
| Codex | `config/codex/config.toml.example` | Optional native `config.toml`; shows verbosity and a custom MCP example |
| Claude Code | `config/claude-code/settings.json.example` | Optional native `settings.json`; does not include a key, relaxed permissions, or hooks |

Before running, change the template base URL, environment variables, and value ranges to what your real service supports:

- Responses API: `"api": "openai-responses"`
- Chat Completions: `"api": "openai-completions"`
- OpenClaw `--model` must match `providers.<name>` and `models[].id` exactly
- Codex and Claude Code do not read OpenClaw models JSON; they use the Responses and Anthropic Messages APIs directly. Model, endpoint, key environment variables, and effort are all set via the CLI
- `--codex-config` and `--claude-settings` are optional native Harness config entry points, not a model registry

Full rules are in the [OpenClaw Harness guide](../harnesses/openclaw-configuration.md#model-config-file).

## Basic Task list

[`tasks/tasks.jsonl`](https://github.com/Liuziyu77/Roller/blob/main/examples/tasks/tasks.jsonl) has one task per line:

| ID | Input | What to verify |
| --- | --- | --- |
| `01_text_only` | Single-turn text | Minimal prompt and text reply |
| `02_local_markdown` | `inputs/product-brief.md` | Read a file and produce `results/name.txt` |
| `03_user_memory_context` | `USER.md` + `MEMORY.md` | Context injection and capture |
| `04_multi_turn` | Two `messages` | Predefined multi-turn in the same Agent session |

These four tasks can run on all seven Harnesses and are the best set for an environment check.

## OpenClaw: basic collection

### 1. Dry-run

```bash
roller collect \
  --tasks examples/tasks/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/config/openclaw/models.json \
  --parallel 4 \
  --dry-run
```

The planned turn counts should be `1, 1, 1, 2` in order.

### 2. Live run

```bash
roller collect \
  --tasks examples/tasks/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/config/openclaw/models.json \
  --parallel 4 \
  --timeout 600 \
  --output runs \
  --run-name examples-openclaw
```

## Codex: run the same Tasks

The service must support the Responses protocol, and `roller-codex:0.149.0` must already be built locally:

```bash
roller collect \
  --harness codex \
  --tasks examples/tasks/tasks.jsonl \
  --model gpt-5.6-sol \
  --api-base "$ROLLER_CODEX_BASE_URL" \
  --api-key-env ROLLER_CODEX_API_KEY \
  --codex-config examples/config/codex/config.toml.example \
  --thinking high \
  --reasoning-summary detailed \
  --parallel 2 \
  --timeout 900 \
  --output runs \
  --run-name examples-codex
```

`--codex-config` can be omitted; the example file only sets text verbosity that Roller does not override, and keeps a commented-out MCP template. The fourth task continues the session with `codex exec resume --last`. Image, API, and reasoning details are in the [Codex Harness guide](../harnesses/codex-configuration.md).

## Claude Code: run the same Tasks

The service must support Anthropic `/v1/messages`, and `roller-claude-code:2.1.222` must already be built locally:

```bash
roller collect \
  --harness claude-code \
  --tasks examples/tasks/tasks.jsonl \
  --model claude-sonnet-4-6-thinking \
  --api-base "$ROLLER_ANTHROPIC_BASE_URL" \
  --api-key-env ROLLER_ANTHROPIC_API_KEY \
  --claude-settings examples/config/claude-code/settings.json.example \
  --thinking high \
  --parallel 2 \
  --timeout 900 \
  --output runs \
  --run-name examples-claude-code
```

`--claude-settings` can be omitted; the example only turns off the commit co-author marker and does not change permission mode, thinking, or tool behavior. The fourth task continues the session with `claude --continue`. Image, endpoint, and thinking are in the [Claude Code Harness guide](../harnesses/claude-code-configuration.md).

## Advanced examples

### Dynamic User Simulator

[`tasks/user-simulator.jsonl`](https://github.com/Liuziyu77/Roller/blob/main/examples/tasks/user-simulator.jsonl) has another OpenClaw Harness play an HR role and add hiring requirements turn by turn from the task Agent's current draft:

```bash
roller collect \
  --tasks examples/tasks/user-simulator.jsonl \
  --model-config examples/config/openclaw/models.json \
  --parallel 1 \
  --timeout 900 \
  --output runs \
  --run-name examples-user-simulator
```

The task already includes `model: my-openai-proxy/my-model`. A dynamic conversation also writes `conversation.json` and `user_simulator/` traces; the Task Agent and Simulator can each choose OpenClaw, Codex, Claude Code, Hermes, OpenCode, DeepSeek, or Pi in their own fields. See [User Simulator](../tasks/user-simulator.md).

### Multimodal input

Use [`config/openclaw/models-multimodal.json`](https://github.com/Liuziyu77/Roller/blob/main/examples/config/openclaw/models-multimodal.json) and declare a local image in the task `files.inputs`:

```json
{
  "schema_version": "roller.task/v1",
  "id": "inspect_image",
  "file_root": "files/inspect_image",
  "prompt": "查看 inputs/example.png，把描述保存到 outputs/description.md。",
  "files": {
    "inputs": ["inputs/example.png"],
    "output_dir": "outputs"
  }
}
```

```bash
roller collect \
  --tasks your-image-tasks.jsonl \
  --model my-openai-proxy/my-multimodal-model \
  --model-config examples/config/openclaw/models-multimodal.json \
  --parallel 2 \
  --output runs \
  --run-name examples-vision
```

Declaring a capability in the model config does not make a text backend support images; the service and model themselves must support visual input under that protocol.

## Check and view

Using the OpenClaw run as an example:

```bash
roller view runs/examples-openclaw
```

Focus on:

- whether the `02_local_markdown` sample contains `results/name.txt`
- whether `04_multi_turn` contains two user messages
- whether each successful sample has `trace.jsonl`, `trace_ACTF.json`, `trace_SFT.json`, and `info.json`
- whether run-root `sft.jsonl` aggregates successful samples in request order

Next: write your own [Task](../tasks/task-format.md), or read [Trajectory outputs](../traces/trajectory-outputs.md) to pick the files you need for training, debugging, and audit.
