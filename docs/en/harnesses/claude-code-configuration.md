# Claude Code Harness guide

This guide covers Claude Code-specific images, the Anthropic Messages API, thinking, native session, and troubleshooting. For Task fields, see [Task format reference](../tasks/task-format.md). For the outputs shared by all Harnesses, see [Trajectory output file guide](../traces/trajectory-outputs.md).

> Jump to: supported scope (section 1) · image (section 2) · API (section 3) · thinking (section 4) · native/ACTF/SFT/wire (sections 8 to 9) · troubleshooting (section 10)

## 1. Current supported scope

The Claude Code Harness supports:

- single-turn `prompt`;
- predefined multi-turn `messages`, with later messages continuing the same session via `claude --continue`;
- Task Bundle, read-only inputs, Context, `runtime.setup`, and full `output_dir` collection;
- parallelism, retry, run lock, and Resume integrity checks;
- Claude Code main session and subagent sidechain native JSONL;
- conversion of plaintext `thinking/reasoning/analysis`, text, multiple `tool_use`, `tool_result`, and usage;
- `ACTF_v1.0`, one SFT record, run-level `sft.jsonl`, and Viewer;
- `--wire-trace actf|full|off`;
- dynamic User Simulator and Task Agent; the two can use different models, and the Simulator
  selects its Harness independently via `user_simulator.harness`;
- Claude Code `settings.json`, `--max-turns`, `MAX_THINKING_TOKENS`, and permission mode;

Not currently supported:

- APIs that only have OpenAI Chat Completions/Responses and no Anthropic `/v1/messages`;
- recovering plaintext from `redacted_thinking` or `thinking.signature`. Opaque values stay in raw,
  are not exported as reasoning, and trigger no extra recovery requests.

## 2. Build a pinned image

Roller uses [`docker/claude-code/Dockerfile`](../../../docker/claude-code/Dockerfile). The base image is
`node:22-bookworm-slim`, and the default pinned install is `@anthropic-ai/claude-code@2.1.222`. The Node 22
offline archive already used by Codex can be reused; you do not need to download the base image again.

Online base image:

```bash
docker pull --platform linux/amd64 node:22-bookworm-slim
docker build \
  --platform linux/amd64 \
  --build-arg CLAUDE_CODE_VERSION=2.1.222 \
  -f docker/claude-code/Dockerfile \
  -t roller-claude-code:2.1.222 \
  .
```

Offline base image:

```bash
docker load -i /path/to/node-22-bookworm-slim-linux-amd64.tar.gz
docker build \
  --platform linux/amd64 \
  --build-arg CLAUDE_CODE_VERSION=2.1.222 \
  -f docker/claude-code/Dockerfile \
  -t roller-claude-code:2.1.222 \
  .
```

For `DEBIAN_MIRROR` and `DEBIAN_SECURITY_MIRROR`, see [Docker builds and package mirrors](../start/docker-builds.md). Set `NPM_REGISTRY` to override npm downloads, or leave it unset to keep the existing npm configuration.

When the default Debian mirrors are unreachable, pass mirror URLs:

```bash
docker build \
  --platform linux/amd64 \
  --build-arg CLAUDE_CODE_VERSION=2.1.222 \
  --build-arg DEBIAN_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian \
  --build-arg DEBIAN_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security \
  -f docker/claude-code/Dockerfile \
  -t roller-claude-code:2.1.222 \
  .
```

Verify and set the default image:

```bash
docker run --rm roller-claude-code:2.1.222 claude --version
export ROLLER_CLAUDE_CODE_IMAGE=roller-claude-code:2.1.222
```

Production datasets should pin the Claude Code version. Native session fields, permission modes, and effort lists evolve
with the CLI. Roller writes the actual `claude_code_version`, image tag, and image ID into run/sample info.

(claude-code-cli-flags)=
## 3. Harness-specific CLI flags, Anthropic API, and Key

This table collects the Claude Code-specific entry points. Generic forms are convenient for a Claude Code-only run, while mixed-Harness runs should use the Claude Code-specific forms.

| Flag | Default | Purpose |
| --- | --- | --- |
| `--docker-image` / `--claude-code-image` | `ROLLER_CLAUDE_CODE_IMAGE` or `roller-claude-code:2.1.222` | Overrides the Claude Code image |
| `--api-base` / `--anthropic-base-url` / `--claude-api-base` | `ANTHROPIC_BASE_URL` | Anthropic API address or full `/v1/messages` endpoint |
| `--api-key-env` / `--claude-api-key-env` | `ANTHROPIC_API_KEY` | Host environment variable containing the API key |
| `--claude-settings` | None | Injects a native `settings.json` into the isolated container |
| `--claude-max-turns` | None | Agent-turn limit for each predefined user message |
| `--claude-max-thinking-tokens` | None | Passes `MAX_THINKING_TOKENS` to Claude Code |
| `--claude-permission-mode` | `bypassPermissions` | Claude Code permission mode |

The retired `--claude-signature-recovery*` flags are no longer accepted. Delete them from old commands.


Claude Code uses the Anthropic Messages API. Put the key in a custom host environment variable, then map it with
`--api-key-env` to `ANTHROPIC_API_KEY` inside the one-shot container:

```bash
export ROLLER_ANTHROPIC_KEY='...'

roller collect \
  --harness claude-code \
  --tasks examples/tasks/tasks.jsonl \
  --model claude-sonnet-4-6-thinking \
  --api-base http://your-api.example/v1/messages \
  --api-key-env ROLLER_ANTHROPIC_KEY \
  --docker-image roller-claude-code:2.1.222 \
  --thinking high \
  --output runs \
  --run-name claude-code-demo
```

`--api-base` and `--anthropic-base-url` are two names for the same flag and accept:

```text
http://host:port
http://host:port/v1
http://host:port/v1/messages
```

Roller strips a trailing `/v1` or `/v1/messages` before writing `ANTHROPIC_BASE_URL`. Claude Code
then requests the single `/v1/messages`, avoiding `/v1/v1/messages`. Claude Code 2.1.222
also appends `?beta=true` and an `anthropic-beta` header on SDK requests. When using Roller's
wire proxy, those two transport-only markers are removed before forwarding to a compatible service; other query
parameters and headers are kept. If you omit the flag, Roller reads the host
`ANTHROPIC_BASE_URL`. The key flag defaults to `ANTHROPIC_API_KEY`.

The key value is not written to the run, lock, raw, or provenance; only the environment variable name of the key is recorded. The API base is
written to metadata after query and URL credentials are stripped. You can preflight the full endpoint with this minimal request:

```bash
curl -sS -X POST 'http://host:port/v1/messages' \
  -H 'Content-Type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -H "x-api-key: $ROLLER_ANTHROPIC_KEY" \
  -d '{
    "model":"claude-sonnet-4-6-thinking",
    "max_tokens":128,
    "messages":[{"role":"user","content":"Reply OK"}]
  }'
```

A model appearing in `/v1/models` does not guarantee the current account has an available channel. Verify access with an actual `/v1/messages` request.

## 4. Thinking and effort

The Claude Code Harness maps `--thinking LEVEL` to `claude --effort LEVEL`:

| Roller input | Claude Code 2.1.222 flag | Notes |
| --- | --- | --- |
| omitted | do not pass `--effort` | Use the CLI/model default behavior |
| `low` | `low` | Low effort |
| `medium` | `medium` | Medium effort |
| `high` | `high` | High effort |
| `xhigh` | `xhigh` | Depends on model support |
| `max` | `max` | Depends on model support |
| `ultra` | `ultracode` | Mapping from the Roller logical name to the pinned CLI name |

`off/none`, `minimal`, and `adaptive` have no matching Claude Code CLI level, so they are rejected before
container startup rather than mapped to another level. The task's `runtime.thinking` wins over the command-line default.

You can also set an explicit thinking token cap:

```text
--claude-max-thinking-tokens 4096
```

It is passed to Claude Code as `MAX_THINKING_TOKENS`. Different models may use budget thinking or adaptive
thinking. If the model/provider does not support that combination, omit this flag and use only `--thinking`.

`thinking`, `reasoning`, or `analysis` blocks that actually exist in the Claude Code native session enter
ACTF/SFT `reasoning_content`. The following cases produce no trainable plaintext:

- the service only returns a thinking token count and leaves the thinking text empty;
- Anthropic returns undecryptable `redacted_thinking`;
- the model only shows its working in the final `text` and has no reasoning block.

Roller does not infer hidden reasoning from token counts, final text, or signatures. `--no-keep-thinking` clears reasoning in ACTF/SFT
but does not modify the raw session or the Claude Code context used by later `--continue`.

## 5. Reasoning retention boundary

Roller saves only visible thinking/summary returned by the service. It does not recover signatures, fill in CoT, or make post-task model calls. `--keep-thinking` preserves that text by default; `--no-keep-thinking` clears ACTF/SFT reasoning and leaves raw unchanged.

## 6. Claude settings and permissions

You can inject an existing settings JSON into each one-shot container:

```bash
roller collect ... \
  --claude-settings examples/config/claude-code/settings.json.example
```

The file is copied to the isolated `$CLAUDE_CONFIG_DIR/settings.json` and loaded explicitly with `--settings`. To avoid copying
settings that may contain environment variables or hooks into the run, the run lock stores only a short BLAKE2b content fingerprint.
If the file changes, Resume refuses to mix in old samples. The repo example contains no key, hook, or permission relaxation; it only turns off the commit
co-author marker. Copy it and then add your own Claude Code settings as needed.

Automated collection defaults to:

```text
--claude-permission-mode bypassPermissions
IS_SANDBOX=1
```

You can choose another permission mode supported by the pinned image. `--claude-max-turns` limits Claude Code agent turns inside each predefined
user message. It is not the same as the task's `messages` count.

## 7. Context, workspace, and multi-turn

The Claude Code container working directory is `/roller/workspace`:

- inputs keep their relative paths from the Task Bundle and are mounted read-only;
- Context files are copied into the workspace; project instructions that Claude Code should load automatically should set the target to
  `CLAUDE.md`, for example `{"role":"system","source":"context/CLAUDE.md","target":"CLAUDE.md"}`;
- `skills/` content with `role=skill` is also copied to the isolated `$CLAUDE_CONFIG_DIR/skills/`;
- `runtime.setup` runs in the workspace before the first turn;
- `files.output_dir` stays writable and is safely collected into the sample `results/` at the end.

The first predefined `messages` item starts a new session; later messages use `--continue`. Each rollout has its own
Docker container and `$CLAUDE_CONFIG_DIR` and does not read host Claude login state, settings, or sessions.

## 8. Trajectory outputs

The default sample layout matches the other Harnesses:

```text
<sample>/
├── trace.jsonl
├── trace_ACTF.json
├── trace_SFT.json
├── info.json
└── results/                 # 有输出时
```

### `trace.jsonl`

Roller selects this rollout's newest main session from `$CLAUDE_CONFIG_DIR/projects/**/<session-id>.jsonl`
and also reads `<session-id>/subagents/*.jsonl`. Events are merged by timestamp, deduplicated by native `uuid`, and saved as
`trace.jsonl`. The event objects are kept without the cleanup applied to training data. `info.json.trajectory.raw_schema` is marked
`claude-code-session-jsonl`.

This is a merged export of the Claude Code native session. It is not:

- the CLI `--output-format=stream-json` terminal display stream;
- the provider's HTTP request or response;
- a byte-for-byte concatenation of the source JSONL files.

Raw keeps plaintext thinking, encrypted thinking envelopes, tool metadata, usage, and sidechain-private
fields. The structure is not guaranteed to be stable across Claude Code versions.

### `trace_ACTF.json`

The converter merges the text, reasoning, and multiple `tool_use` items of one inference into a single
ACTF step by Claude `message.id`, then attaches later `tool_result` items by `tool_use_id`. Both the main session and subagent sidechains
enter the trajectory in time order. Sidechain steps are marked with `extra.is_sidechain`.

Usage uses the last value for the same `message.id` so stream updates are not double-counted:

- `input_tokens`
- `cache_read_input_tokens`
- `cache_creation_input_tokens`
- `output_tokens`

Each successful result's `total_cost_usd` from the CLI stream is accumulated into sample usage.

### `trace_SFT.json` and `sft.jsonl`

The Collector converts ACTF into a training record containing only `messages` and `tools`. Assistant messages use this format:

```json
{
  "role": "assistant",
  "reasoning_content": "visible thinking",
  "content": "visible answer",
  "tool_calls": []
}
```

Native Claude `tool_use` becomes an OpenAI-compatible function call; `tool_result` becomes the matching `role=tool`
message. If wire captured a complete Anthropic tool schema, `input_schema` is normalized to
`function.parameters`; otherwise a permissive schema is generated from the actual call arguments.

## 9. Wire trace

When a custom `--api-base` is set, Claude Code's default `--wire-trace actf` starts a rollout-local
reverse proxy, temporarily capturing the first system prompt, the full tools schema, and non-message request parameters. After ACTF/SFT
are generated, the intermediate `wire_trace.jsonl` is deleted to reduce storage use.

To check the JSON context the model actually received on each call, use:

```text
--wire-trace full
```

The sample then also keeps `wire_trace.jsonl`, which contains the system, messages, tools, thinking/output config, and sampling
parameters of each Anthropic `/v1/messages` request. It records the request body forwarded to the service,
not the provider response. Multi-turn requests repeat history, so they should not be used as SFT data directly.
If a compatibility rewrite occurs, `path` keeps Claude Code's original request target, `upstream_path`
records the actual forwarded target, and `dropped_request_headers` records only the removed header names, not their values.

`--wire-trace off` does not start the proxy. The native session can still produce raw/ACTF/SFT, but the top-level
`system_prompt` is empty and the tools schema can only be inferred from actual calls. The wire proxy requires the container to use the default
`--network host`.

## 10. Verified behavior and troubleshooting

During implementation the following protocol probes were run against an Anthropic endpoint and Claude Code 2.1.207. The production image is pinned at
2.1.222:

- `/v1/messages` authentication and ordinary text responses work;
- a `claude-sonnet-4-6-thinking` + `--effort high` native session stores plaintext thinking;
- Bash and Read tool calls and results enter the native session;
- a second CLI invocation with `--continue` reuses the same session;
- the merged ACTF/SFT keep two user messages, thinking, three tool actions, and the final reply.

Common errors:

- `Invalid token`: check the host variable pointed to by `--api-key-env`;
- `No available channel for model`: the model appears in the list, but the current account/group has no available route;
- URL shows `/v1/v1/messages`: do not pass `ANTHROPIC_BASE_URL` into the container by hand; use Roller's
  `--api-base` so it can normalize;
- `invalid beta flag`: use an explicit `--api-base` and the default `--wire-trace actf/full`;
  Roller removes the Claude SDK `beta=true` query and
  `anthropic-beta` header only when forwarding upstream;
- the model rejects `budget_tokens` or requires adaptive thinking: remove
  `--claude-max-thinking-tokens` and use only a `--thinking` effort that model supports;
- ACTF has thinking tokens but `reasoning_content` is empty: the service did not return plaintext; the converter did not drop it;
