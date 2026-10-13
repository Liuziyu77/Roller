# Codex Harness guide

This guide covers Codex-specific images, the Responses API, reasoning, native session, and troubleshooting. For Task fields, see [Task format reference](../tasks/task-format.md). For the outputs shared by all Harnesses, see [Trajectory output file guide](../traces/trajectory-outputs.md).

## 1. Scope

The Codex Harness supports:

- single-turn `prompt`.
- predefined multi-turn `messages`, with later messages continuing the same session via `codex exec resume --last`.
- Task Bundle, read-only inputs, Context, `runtime.setup`, and full `output_dir` collection.
- parallelism, retry, run lock, and Resume integrity checks.
- Codex native session JSONL, `ACTF_v1.0`, one SFT record, and run-level `sft.jsonl`.
- original request-side tool capture in default `--wire-trace actf`, plus full requests with `--wire-trace full`.
- local-image viewing through the native `view_image` tool, with multimodal trajectory export.
- dynamic User Simulator and Task Agent. The Simulator can be selected independently via `user_simulator.harness`
  and can use a different model.
- Viewer display of visible reasoning, tool calls, tool results, and the final reply.

Not currently supported:

- OpenClaw-specific cache trace.
- image blocks embedded directly in Task `messages`.
- internal reasoning that the API does not return as plaintext or a visible summary.
- model services that only provide Chat Completions and lack the Responses interface Codex needs.

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
docker load -i /path/to/node-22-bookworm-slim-linux-amd64.tar.gz
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

You can select another already-built image via CLI or environment variable:

```bash
export ROLLER_CODEX_IMAGE=roller-codex:0.149.0
# Or: roller collect ... --docker-image roller-codex:0.149.0
```

Do not use an unlocked `@openai/codex@latest` in a production dataset. Codex native events evolve with the
CLI. Roller writes the actual `codex_version`, image tag, and image ID into run/sample info and the run lock.

## 3. Environment variables and API

The Codex Harness uses a Responses-style agentic API. The service must correctly handle Responses streaming, reasoning, tool calls, and tool results. A successful `/v1/chat/completions` request does not show that a service can run Codex.

| Setting | Default source | CLI override |
| --- | --- | --- |
| API address | `OPENAI_BASE_URL` | `--codex-api-base`, or `--api-base` for a single-Harness run |
| API key | `OPENAI_API_KEY` | `--codex-api-key-env`, or `--api-key-env` for a single-Harness run |
| Model ID | The task `model` or `--model` | Passed to Codex unchanged |
| API transport | Codex built-in policy | A custom API address uses Responses HTTP streaming by default |

Store the key in a custom host environment variable and pass its name to Roller:

```bash
export ROLLER_CODEX_KEY='...'

roller collect \
  --harness codex \
  --tasks examples/tasks/tasks.jsonl \
  --model gpt-5.6-sol \
  --api-base http://your-api.example/v1 \
  --api-key-env ROLLER_CODEX_KEY \
  --docker-image roller-codex:0.149.0 \
  --output runs \
  --run-name codex-gpt
```

When `--api-base` is omitted, Roller reads `OPENAI_BASE_URL` from the host. The value of `--api-key-env` is an environment variable name, not the key itself. The key is not written to `info.json` or `lock.json`. The run lock records only the variable name, and provenance removes query data and credentials from the API address.

With a custom API address, Roller creates an isolated Codex provider with `wire_api="responses"` and `supports_websockets=false` by default. This works with compatible services that implement Responses HTTP streaming only. Enable `--codex-websockets` only when the service supports Responses WebSocket.

`--wire-trace actf` (default) and `full` capture tools through a rollout-local HTTP proxy when an API address is configured. Capture requires `--network host` and the default `--no-codex-websockets`. Without an API address, Codex uses its built-in provider; native raw/ACTF remain available, but SFT is unavailable because the original tool definitions cannot be captured. `full` rejects missing API addresses or WebSocket transport.

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

The file is copied unchanged to `$CODEX_HOME/config.toml` in the rollout container. The Resume lock stores its SHA-256 instead of copying potentially sensitive TOML content into the run. Explicit CLI values for the API address, model, effort, summary, and web search still override matching runtime settings. The example includes `model_verbosity`, which Roller does not override, and a commented MCP server template. Do not store API keys in this file.

## 5. Multimodal capabilities

The Codex Harness supports both text-only models and vision-capable Responses models. Images enter the workspace through `files.inputs`, and Codex can open local images with its native `view_image` tool. Image tool results are preserved in raw, ACTF, and multimodal SFT, where SFT uses `<image>` placeholders and the top-level `images` field.

Vision tasks also require the Codex version, selected model, and Responses API service to support image input. Task `messages[].content` currently accepts strings only and cannot contain image blocks directly. Text-only models can continue to process text and ordinary files, but they cannot complete tasks that require understanding image content.

## 6. Thinking configuration

In the Codex Harness, `--thinking LEVEL` maps to:

```toml
model_reasoning_effort = "LEVEL"
```

For example:

```bash
roller collect ... \
  --harness codex \
  --thinking xhigh \
  --reasoning-summary detailed
```

The task's `runtime.thinking` wins over the CLI. Roller forwards
`minimal/low/medium/high/xhigh/max/ultra` to Codex. The model and API service then validate them; support depends on
the selected model and endpoint. Roller does not infer support from the Codex CLI version. The OpenClaw-only
`adaptive` is also rejected early. `off`/`none` are rejected as well, because the Codex Harness has no
single switch that turns off reasoning for every reasoning model. To use the model/CLI default,
omit `--thinking`.

`--reasoning-summary` accepts:

- `auto`
- `concise`
- `detailed`
- `none`

A Codex native session may store plaintext `response_item.reasoning.content`, a visible
`response_item.reasoning.summary`, and non-public encrypted reasoning. Roller prefers whatever plaintext
content actually exists and keeps the summary when plaintext is absent. Non-public content remains only in native raw. OpenAI reasoning
models usually expose only a summary. Open-source Responses services may return plaintext reasoning content
directly. Choosing `none` means a missing summary is expected, but if the service still returns plaintext content, Roller
saves it according to `--keep-thinking`.

If an open-source Responses model returns plaintext reasoning content, the default `--keep-thinking` projects it
directly into ACTF/SFT `reasoning_content`.

`--no-keep-thinking` clears `reasoning_content` during native raw→ACTF. The Codex native
`trace.jsonl` stays as-is, and this option currently does not rewrite the context Codex itself uses for later `resume`.
It is therefore an export control, not a raw-redaction or two-way context-stripping switch.

## 7. Web Search

To reduce unreproducible external dependencies, the Codex Harness defaults to:

```text
--codex-web-search disabled
```

You can set it explicitly to `cached` or `live`. When a native session contains `web_search_end`, Roller turns
the query/action and recorded results into a `web_search` tool call/result for ACTF, SFT, and
the Viewer.

## 8. Sessions and workspace

The Codex container working directory is `/roller/workspace`:

- inputs keep their relative paths from the Task Bundle and are mounted read-only.
- Context files are copied into the workspace. `AGENTS.md` takes effect under Codex's normal discovery rules.
- `skills/` content with `role=skill` is also copied to `$HOME/.agents/skills/`.
- `runtime.setup` runs in the workspace before the first Codex turn.
- `files.output_dir` stays writable inside the workspace and is safely collected into the sample `results/` at the end.

The first predefined `messages` call uses `codex exec`. Later messages use `codex exec resume --last` and share
the same isolated `$CODEX_HOME`. Each rollout uses its own container and Codex home and does not read host
`~/.codex` accounts, sessions, or global config.

### User Simulator

Codex can be the Task Agent or the User Simulator. Set
`"harness": "codex"` at the task top level for the Task Agent, and set `user_simulator.harness` for the Simulator. The two
`model` values can also be specified separately. Dynamic sessions keep context with Codex `resume --last`.

## 9. Trajectories and request recording

The default Codex sample layout matches OpenClaw:

```text
<sample>/
├── trace.jsonl
├── trace_ACTF.json
├── tools_capture.jsonl      # Original per-request tool definitions
├── trace_SFT.json
├── info.json
└── results/                 # When output exists
```

### `trace.jsonl`

This is the newest native session file under `$CODEX_HOME/sessions/<date>/rollout-*.jsonl`. It keeps
the Codex event structure, including `session_meta`, `turn_context`, `response_item`, `event_msg`, compaction events,
and token usage. It is a Codex-private schema. It is not promised to be stable across CLI versions, and it is not a per-request HTTP wire
trace.

### `trace_ACTF.json`

The converter uses `token_count` as the boundary of one model request, combines separate reasoning, assistant message,
and multiple function/custom tool calls into one ACTF step, then pairs later tool
output by `call_id`. The top-level system prompt comes from `session_meta.base_instructions`, native developer/system
messages, and the `<environment_context>` Codex injects before the first turn. User messages from the task are matched by
content to avoid treating environment events as user messages or assigning them to the wrong turn.

Tool definitions come from the actual outgoing Responses requests, including tools never called by the model. Roller does not infer schemas from arguments. ACTF records `tools_format: "responses"`, the unmodified `tools` list, and `tools_capture` provenance with every request snapshot. Definitions, descriptions, required fields, enums, custom grammars, namespaces, and original ordering are preserved.

`tools_capture.jsonl` survives normal sample pruning. It contains only tool-related fields (`tools`, `tool_choice`, `parallel_tool_calls`, and any `input` items of type `additional_tools`), without ordinary messages or HTTP headers. Schema contents are preserved verbatim as JSON values, including properties named `token`; they are not subjected to the generic sensitive-key redactor. Treat descriptions and defaults as original harness-provided content.

On shutdown, capture ends with a `codex_tools_complete` record containing the Responses `request_count`. SFT requires this final record and a matching snapshot count. Truncated captures and older captures without completion evidence retain raw/ACTF and the available snapshots, but cannot produce SFT.

### `trace_SFT.json` and `sft.jsonl`

A text-only SFT record contains `messages` and `tools`; image records also contain `images`. Codex `tools` retains the native Responses format, including namespace/custom/built-in entries, instead of normalizing everything to nested Chat Completions functions. Custom calls retain their string `input` under a `type: "custom"` call; namespace fields are preserved. The assistant always includes
`reasoning_content`, `content`, and `tool_calls`. Tool results become `role=tool`. Run-level `sft.jsonl`
only aggregates successful or trusted Resume samples.

Missing or invalid captures, changing tool definitions across requests, or programmatic `additional_tools` input items make SFT explicitly unavailable. The raw trajectory and exact request snapshots remain available. A single top-level SFT `tools` list cannot faithfully represent these changing or alternative definitions. Even a trajectory without tool calls needs capture; only a recorded request with empty/omitted tools proves that its tool list was empty. `--wire-trace off` therefore produces no Codex SFT.

### Native raw and wire trace

Codex may insert instructions, compact history, selectively replay
tool results, or convert internal events into Responses input between the native session and the real model request. `trace.jsonl` and the ACTF/SFT generated from it can only
prove what the Codex session recorded, not the full JSON context each model call finally received.

The Codex Harness supports `--wire-trace full`: Roller temporarily points the custom provider's API base at a
rollout-local proxy and saves a redacted full JSON request body before forwarding to the real `--api-base`.
Tool definitions can appear in an `additional_tools` item of `input`. The file is request-side evidence. It does not save
the upstream response body, and it cannot say how the model service later truncates, routes, or tokenizes internally.

Default `--wire-trace actf` keeps `tools_capture.jsonl` and removes the temporary wire file after conversion. It captures original tool definitions without storing full message bodies. To inspect the full request context for a model call, use `--wire-trace full` and appropriate log access controls. Tool capture observes the request sent to the API; it cannot verify any transformations inside the upstream service.

## 10. Common issues

**(1) `Codex requires a non-empty API key`**

The host variable pointed to by `--api-key-env` is missing or empty. Check the variable name. Do not put the key directly in the task.

**(2) `/responses` returns 404 or a protocol error**

The API service only has Chat Completions, or its Responses streaming/tool protocol is incompatible. Switch to a
Responses-capable service. The OpenClaw Harness can still be used for model services that only support Chat Completions.

**(3) `Codex native trace produced no assistant steps`**

The CLI exited before producing a valid model event, or the native event schema of the Codex version in use has changed. First inspect
the error kept on the sample and the `--keep-container-on-error` container, then check `codex_version`. When upgrading the CLI,
update the converter tests as well.
