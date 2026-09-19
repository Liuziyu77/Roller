# Codex Harness guide

This guide covers Codex-specific images, the Responses API, reasoning, native session, and troubleshooting. For Task fields, see [Task format reference](../tasks/task-format.md). For the outputs shared by all Harnesses, see [Trajectory output file guide](../traces/trajectory-outputs.md).

> Jump to: supported scope (section 1) · image (section 2) · API (section 3) · reasoning (section 4) · native/ACTF/SFT/wire (sections 7 to 8) · troubleshooting (section 9)

## 1. Current supported scope

The Codex Harness supports:

- single-turn `prompt`;
- predefined multi-turn `messages`, with later messages continuing the same session via `codex exec resume --last`;
- Task Bundle, read-only inputs, Context, `runtime.setup`, and full `output_dir` collection;
- parallelism, retry, run lock, and Resume integrity checks;
- Codex native session JSONL, `ACTF_v1.0`, one SFT record, and run-level `sft.jsonl`;
- request-side `--wire-trace full`;
- dynamic User Simulator and Task Agent; the Simulator can be selected independently via `user_simulator.harness`
  and can use a different model;
- Viewer display of visible reasoning, tool calls, tool results, and the final reply.

Not currently supported:

- OpenClaw-specific cache trace;
- recovering unpublished or encrypted full internal reasoning from the native session;
- model services that only provide Chat Completions and lack the Responses interface Codex needs.

## 2. Build a pinned image

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
# 或 roller collect ... --docker-image roller-codex:0.149.0
```

Do not use an unlocked `@openai/codex@latest` in a production dataset. Codex native events evolve with the
CLI. Roller writes the actual `codex_version`, image tag, and image ID into run/sample info and the run lock.

## 3. API and Key

Put the key in a custom host environment variable, then map it with `--api-key-env` to a short-lived
`OPENAI_API_KEY` inside the container:

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

If `--api-base` is omitted, Roller reads the host `OPENAI_BASE_URL`. The key value is not written to `info.json` or
`lock.json`; the run lock only records the environment variable name of the key. The API base is written to
provenance after query and credentials are stripped.

When `--api-base` is provided, Roller creates an isolated Codex custom provider that pins
`wire_api="responses"` and defaults to `supports_websockets=false`. This works with OpenAI-compatible services that
only implement Responses HTTP streaming and have no WebSocket handshake. Add `--codex-websockets` only when you have
confirmed the service supports Responses WebSocket. `--wire-trace full` uses an HTTP proxy for each rollout, so you must pass `--api-base` explicitly, keep
`--no-codex-websockets`, and keep the default `--network host`. When `--api-base` is unset, Codex uses its own
built-in OpenAI provider and transport policy.

The Codex CLI uses a Responses-style agentic API. A successful `curl /v1/chat/completions` does not prove the
service can be used by Codex. The service must also correctly handle the Responses streaming, reasoning,
tool call, and tool result traffic that Codex emits. For a special model provider, MCP, or other Codex config, you can pass:

```bash
roller collect ... \
  --codex-config examples/config/codex/config.toml.example
```

The file is copied as-is to `$CODEX_HOME/config.toml` in the rollout container. The Resume lock stores a
SHA-256 of the config contents and does not copy possibly secret TOML into the run. Explicit CLI API base, model, effort,
summary, and web-search settings still override the corresponding run items. The repo example sets
`model_verbosity`, which Roller does not override, and includes a default commented-out MCP server template. Do not store API keys in it.

## 4. Reasoning configuration

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
`minimal/low/medium/high/xhigh` to Codex; the model and API service then validate them. `xhigh` depends on
whether the model supports it. Codex CLI 0.149.0 does not accept `max`/`ultra`; Roller errors early. The OpenClaw-only
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
content actually exists; if there is no plaintext it keeps the summary. It does not decrypt, guess, or fabricate internal CoT. OpenAI reasoning
models usually expose only a summary. Open-source Responses services may return plaintext reasoning content
directly. Choosing `none` means a missing summary is expected, but if the service still returns plaintext content, Roller
saves it according to `--keep-thinking`.

If an open-source Responses model returns plaintext reasoning content, the default `--keep-thinking` projects it
directly into ACTF/SFT `reasoning_content`.

`--no-keep-thinking` clears `reasoning_content` during native raw→ACTF. The Codex native
`trace.jsonl` stays as-is, and this option currently does not rewrite the context Codex itself uses for later `resume`.
It is therefore an export control, not a raw-redaction or two-way context-stripping switch.

## 5. Web search

To reduce unreproducible external dependencies, the Codex Harness defaults to:

```text
--codex-web-search disabled
```

You can set it explicitly to `cached` or `live`. When a native session contains `web_search_end`, Roller turns
the query/action and recorded results into a `web_search` tool call/result for ACTF, SFT, and
the Viewer.

## 6. Context and workspace

The Codex container working directory is `/roller/workspace`:

- inputs keep their relative paths from the Task Bundle and are mounted read-only;
- Context files are copied into the workspace; `AGENTS.md` takes effect under Codex's normal discovery rules;
- `skills/` content with `role=skill` is also copied to `$HOME/.agents/skills/`;
- `runtime.setup` runs in the workspace before the first Codex turn;
- `files.output_dir` stays writable inside the workspace and is safely collected into the sample `results/` at the end.

The first predefined `messages` call uses `codex exec`; later messages use `codex exec resume --last` and share
the same isolated `$CODEX_HOME`. Each rollout uses its own container and Codex home and does not read host
`~/.codex` accounts, sessions, or global config.

## 7. Trajectory outputs

The default Codex sample layout matches OpenClaw:

```text
<sample>/
├── trace.jsonl
├── trace_ACTF.json
├── trace_SFT.json
├── info.json
└── results/                 # 有输出时
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

A Codex native session does not provide a complete original JSON Schema for each tool. To emit strict SFT, Roller
generates a permissive function schema from the actual call names and argument fields. These schemas preserve tool-call names,
arguments, and results, but do not contain the full definitions of Codex's built-in tool interfaces.

### `trace_SFT.json` and `sft.jsonl`

As with OpenClaw, a single SFT record's top level is strictly `messages` and `tools`. The assistant always includes
`reasoning_content`, `content`, and `tool_calls`. Tool results become `role=tool`. Run-level `sft.jsonl`
only aggregates successful or trusted Resume samples.

## 8. Native raw is not a wire trace

Codex may insert instructions, compact history, selectively replay
tool results, or convert internal events into Responses input between the native session and the real model request. `trace.jsonl` and the ACTF/SFT generated from it can only
prove what the Codex session recorded, not the full JSON context each model call finally received.

The Codex Harness supports `--wire-trace full`: Roller temporarily points the custom provider's API base at a
rollout-local proxy and saves a redacted full JSON request body before forwarding to the real `--api-base`.
Tool definitions can appear in an `additional_tools` item of `input`. The file is request-side evidence. It does not save
the upstream response body, and it cannot say how the model service later truncates, routes, or tokenizes internally.

Default `--wire-trace actf` does not keep an extra wire file. The Codex ACTF tool schema comes from a permissive projection of
native actual calls, not the full request schema. To inspect the full request for a model call,
you must pass `--wire-trace full` explicitly and pair it with sufficiently strict log access controls.

## 9. Common errors

### `Codex requires a non-empty API key`

The host variable pointed to by `--api-key-env` is missing or empty. Check the variable name. Do not put the key directly in the task.

### `/responses` returns 404 or a protocol error

The API service only has Chat Completions, or its Responses streaming/tool protocol is incompatible. Switch to a
Responses-capable service. The OpenClaw Harness can still be used for model services that only support Chat Completions.

### `Codex native trace produced no assistant steps`

The CLI exited before producing a valid model event, or the native event schema of the Codex version in use has changed. First inspect
the error kept on the sample and the `--keep-container-on-error` container, then check `codex_version`. When upgrading the CLI,
update the converter tests as well.

### User Simulator

Codex can be the Task Agent or the User Simulator. Set
`"harness": "codex"` at the task top level for the Task Agent, and set `user_simulator.harness` for the Simulator. The two
`model` values can also be specified separately. Dynamic sessions keep context with Codex `resume --last`.
