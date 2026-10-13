# Codex Harness guide

The Codex Harness runs tasks through the Codex CLI when they need terminal, file, or image tools. It connects to a model service that supports the Responses API. This guide follows the order needed for a first run: image, API, Harness-specific flags, sessions, and outputs.

Roller currently pins Codex 0.149.0. For Task fields, see [Task format reference](../tasks/task-format.md). For shared outputs, see [Trajectory output file guide](../traces/trajectory-outputs.md).

For a first run, complete sections 1 through 3 and run the example command. Read the later sections on Harness-specific flags, thinking, and trajectories when those features are needed.

## 1. Scope

Use the Codex Harness when a task needs:

- a model service that implements the Responses API.
- shell commands, file editing, or local-image inspection through `view_image`.
- single-turn, multi-turn, Task Agent, or User Simulator execution.
- the native Codex session plus ACTF, SFT, and token-usage export.

An endpoint that only supports Chat Completions cannot be used directly. Task messages also cannot contain image blocks directly. Roller can preserve only visible reasoning or summaries returned by the API, not hidden reasoning.

## 2. Image preparation

Roller uses [`docker/codex/Dockerfile`](../../../docker/codex/Dockerfile). The base image is
`node:22-bookworm-slim`, and the default pinned install is `@openai/codex@0.149.0`. The image also includes Git, ripgrep,
Python, curl, jq, an SSH client, and other common task tools.

Online environment:

```bash
docker pull --platform linux/amd64 node:22-bookworm-slim
docker build \
  --platform linux/amd64 \
  --build-arg CODEX_VERSION=0.149.0 \
  -f docker/codex/Dockerfile \
  -t roller-codex:0.149.0 \
  .
```

Offline base image:

```bash
docker load -i node-22-bookworm-slim-linux-amd64.tar.gz
docker build \
  --platform linux/amd64 \
  --build-arg CODEX_VERSION=0.149.0 \
  -f docker/codex/Dockerfile \
  -t roller-codex:0.149.0 \
  .
```

For `DEBIAN_MIRROR` and `DEBIAN_SECURITY_MIRROR`, see [Docker builds and package mirrors](../start/docker-builds.md). Set `NPM_REGISTRY` to override npm downloads, or leave it unset to keep the existing npm configuration.

If the default Debian mirrors are slow on the current network, override the mirror URLs at build time without
editing the Dockerfile:

```bash
docker build \
  --platform linux/amd64 \
  --build-arg CODEX_VERSION=0.149.0 \
  --build-arg DEBIAN_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian \
  --build-arg DEBIAN_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security \
  -f docker/codex/Dockerfile \
  -t roller-codex:0.149.0 \
  .
```

Verify:

```bash
docker run --rm roller-codex:0.149.0 codex --version
```

If the build machine and the collection machine do not share the same Docker daemon, export the image and load it on the target worker:

```bash
docker save -o roller-codex-0.149.0-linux-amd64.tar roller-codex:0.149.0
docker load -i roller-codex-0.149.0-linux-amd64.tar
```

Another already-built image can be selected through a command-line option or environment variable:

```bash
export ROLLER_CODEX_IMAGE=roller-codex:0.149.0
# Or: roller collect ... --docker-image roller-codex:0.149.0
```

Do not use the automatically changing `@openai/codex@latest` for a production collection. Roller writes the
actual `codex_version`, image tag, and image ID to the run and sample `info.json` files so the environment can be checked later.

## 3. Environment variables and API

The Codex Harness needs a model service that implements the Responses API. A first run requires an API address, an API key, and a model ID. A working `/v1/chat/completions` route alone does not mean Codex can run.

| Setting | Default source | CLI override |
| --- | --- | --- |
| API address | `OPENAI_BASE_URL` | `--codex-api-base`, or `--api-base` for a single-Harness run |
| API key | `OPENAI_API_KEY` | `--codex-api-key-env`, or `--api-key-env` for a single-Harness run |
| Model ID | The task `model` or `--model` | Sent to Codex unchanged |

Store the key in a host environment variable and pass its name to Roller:

```bash
export ROLLER_CODEX_KEY='...'

roller collect \
  --harness codex \
  --tasks examples/tasks/tasks.jsonl \
  --model model-id \
  --api-base https://api.example.com/v1 \
  --api-key-env ROLLER_CODEX_KEY \
  --docker-image roller-codex:0.149.0 \
  --limit 1 \
  --output runs \
  --run-name codex-demo
```

`--api-key-env` accepts an environment-variable name, not the key itself. With a custom API address, Roller uses Responses HTTP streaming by default. Keep the default `--no-codex-websockets` unless the service explicitly supports Responses WebSocket.

Keep the default `--wire-trace actf` and `--network host` in the example command so Roller can record the tool definitions needed for ACTF/SFT. If request recording is disabled, Roller still produces the native Codex trace and ACTF, but does not export SFT without complete tool definitions.

(codex-cli-flags)=
## 4. Codex-specific flags

This table lists Codex-specific runtime flags only. See section 3 for API addresses and keys. See the [CLI reference](../start/cli-reference.md) for flags shared by all Harnesses.

| Flag | Default | Purpose |
| --- | --- | --- |
| `--docker-image` / `--codex-image` | `ROLLER_CODEX_IMAGE` or `roller-codex:0.149.0` | Override the Codex image |
| `--codex-config` | None | Inject a native `config.toml` into the isolated container |
| `--reasoning-summary` | Codex default | `auto`, `concise`, `detailed`, or `none` |
| `--codex-web-search` | `disabled` | `disabled`, `cached`, or `live` |
| `--codex-websockets` / `--no-codex-websockets` | Off | Whether a custom provider uses Responses WebSocket |

A single Codex run can use the shared flag forms. In a mixed-Harness run, use dedicated forms such as `--codex-image`, `--codex-api-base`, and `--codex-api-key-env` so the settings apply only to the intended participant.

For a custom model provider, MCP server, or another native Codex setting, provide:

```bash
roller collect ... \
  --codex-config examples/config/codex/config.toml.example
```

Roller copies this file into the task container. Command-line values for the API address, model, thinking effort, reasoning summary, and Web Search take priority over matching file settings. The example also contains `model_verbosity` and a disabled MCP server template. Use a new `--run-name` after changing the file, and do not store API keys in it.

## 5. Multimodal capabilities

The Codex Harness supports both text-only models and vision-capable Responses models. Images enter the workspace through `files.inputs`, and Codex can open local images with its native `view_image` tool. Image tool results are preserved in the native trace, ACTF, and multimodal SFT, where SFT uses `<image>` placeholders and the top-level `images` field.

Vision tasks also require the Codex version, selected model, and Responses API service to support image input. Task `messages[].content` currently accepts strings only and cannot contain image blocks directly. Text-only models can continue to process text and ordinary files, but they cannot complete tasks that require understanding image content.

## 6. Thinking configuration

`--thinking` sets Codex reasoning effort. If omitted, Codex, the model, and the service use their defaults.

Example:

```bash
roller collect ... \
  --harness codex \
  --thinking high \
  --reasoning-summary detailed
```

The Codex Harness passes `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, and `ultra` to Codex as reasoning effort. Actual support depends on the model and API service. `off`, `none`, and the OpenClaw-specific `adaptive` value are not supported. For a new model, omit `--thinking` or begin with a level explicitly supported by the service.

`--reasoning-summary` controls visible reasoning summaries and accepts `auto`, `concise`, `detailed`, or `none`. If the service returns plaintext reasoning, Roller prefers that content. Encrypted-only reasoning does not appear in ACTF/SFT.

`--no-keep-thinking` clears ACTF/SFT `reasoning_content` only. It does not change the native Codex session or continuation context.

## 7. Web Search

Codex disables Web Search by default. Change it only for tasks that need online information:

| Value | Behavior |
| --- | --- |
| `disabled` | Do not use Web Search. This is the default |
| `cached` | Prefer existing search results |
| `live` | Allow live web queries |

Example:

```text
--codex-web-search live
```

Searches are stored as tool calls in the trajectory, so the query and returned results can be inspected in Viewer.

## 8. Sessions and workspace

Each Agent or Simulator runs in an isolated disposable container and does not read host Codex accounts, configuration, or session history.

- Inputs are read-only and keep their Task Bundle relative paths.
- Context files are copied to their declared task paths. `AGENTS.md` follows the normal Codex discovery rules.
- `runtime.setup` runs before the first turn.
- The output directory remains writable and is collected into the sample `results/` directory.

Predefined multi-turn messages continue one Codex session, so conversation history and workspace files remain available in later turns.

### 8.1 User Simulator

Codex can act as either the Task Agent or User Simulator. Configure the Harness and model separately at the task top level and under `user_simulator`. Each role keeps its own session in an isolated container.

## 9. Trajectories and request recording

A run produces these main files:

| File | Purpose |
| --- | --- |
| `trace.jsonl` | Records native Codex session events for troubleshooting |
| `trace_ACTF.json` | Normalizes reasoning, replies, tool calls, and results |
| `tools_capture.jsonl` | Preserves the tool definitions used in actual model requests |
| `trace_SFT.json` | Converts a successful trajectory into training-data format |

### 9.1 `trace.jsonl`

This file preserves native Codex events, context, and token usage for troubleshooting. It is not the complete HTTP request sent to the model service, and its format may change with Codex versions.

### 9.2 `trace_ACTF.json`

ACTF groups reasoning, text, and tool calls from one response and pairs each result with its call. The system prompt and tool definitions come from the actual run.

### 9.3 `trace_SFT.json` and `sft.jsonl`

SFT keeps the Responses tool format used by Codex. Image samples add an `images` field. If tool definitions are missing, corrupt, or change across requests, Roller keeps the native trajectory but does not generate a potentially inaccurate SFT record.

### 9.4 Complete request records

The default `--wire-trace actf` keeps only the tool information needed for ACTF/SFT. To inspect the complete request received by the model, temporarily use:

```text
--wire-trace full
```

The sample then retains complete request records. They can contain message text, tool arguments, and reasoning settings, so use them only for troubleshooting or auditing. `--wire-trace off` records no request tool definitions. Codex still produces its native trace and ACTF, but does not generate SFT without complete tool definitions.

## 10. First-run check

Keep `--limit 1` in the example command for the first run. After it finishes, check:

1. `info.json` in the run directory has `state` set to `finished`.
2. The sample `info.json` has `status` set to `ok`.
3. The sample contains `trace.jsonl`, `trace_ACTF.json`, and `trace_SFT.json`.
4. If the task creates files, the expected files exist under `results/`.

## 11. Common issues

**(1) `Codex requires a non-empty API key`**

The host variable pointed to by `--api-key-env` is missing or empty. Check the variable name. Do not put the key directly in the task.

**(2) `/responses` returns 404 or a protocol error**

The API service only has Chat Completions, or its Responses streaming/tool protocol is incompatible. Switch to a
Responses-capable service. The OpenClaw Harness can still be used for model services that only support Chat Completions.

**(3) `Codex native trace produced no assistant steps`**

Codex exited before producing a valid reply. Inspect the sample `info.json` and `trace.jsonl` first. For further troubleshooting, use `--keep-container-on-error` to retain the failed container and check the `codex_version` recorded in `info.json`.
