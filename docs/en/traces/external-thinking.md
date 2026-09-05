# External Thinking: persist explicit CoT as a tool call

[Documentation home](../index.md) · [OpenClaw](../harnesses/openclaw-configuration.md) · [Codex](../harnesses/codex-configuration.md) · [SFT format](sft-format.md)

Following [oh-my-pi](https://github.com/can1357/oh-my-pi), Roller implements
`external` and `external_always` for OpenClaw and Codex: native provider
reasoning is turned off, the intermediate reasoning the model generates
explicitly is stored in `deep_think` tool arguments, and dedicated SFT then
normalizes it to `reasoning_content`.

This mode is aimed at the GPT reasoning series, for example `gpt-5.6-sol`.
Native Responses reasoning from these models usually gives the client only a
compacted summary. If an open model such as Qwen or GLM already returns
plaintext `reasoning_content`, `reasoning`, or `reasoning_text` through the API,
keep the default `--keep-thinking` to retain CoT. You do not need external
thinking.

## 1. Why it is needed

An OpenAI Responses reasoning item can contain reasoning tokens, opaque/encrypted
state, and a visible summary, but the visible text the client receives is usually
a compacted summary. Roller's native `--thinking` faithfully stores that summary.
It does not invert hidden tokens back into a full CoT.

External thinking uses a different protocol: native reasoning is turned off, and
the model writes the reasoning text it is willing to expose for training into an
ordinary function call:

```text
assistant -> deep_think({"thoughts": "..."})
tool      -> ------
assistant -> business tool call or final answer
```

These thoughts are explicit model output. They are not a break or recovery of
OpenAI hidden reasoning.

## 2. Requirements

Shared requirement: the model service must implement the OpenAI Responses
streaming, tool choice, and tool history replay that Codex/OpenClaw actually
use. Having only `/v1/chat/completions` is not enough.

OpenClaw path requirements:

- The default official image `ghcr.io/openclaw/openclaw:2026.7.1-2`, or a newer
  build compatible with its plugin contract;
- The selected provider in `--model-config` uses `"api": "openai-responses"`;
- The provider has an absolute `baseUrl`, because Roller must apply protocol
  control at the final HTTP boundary;
- The model service supports Responses function tools, named `tool_choice`, and
  function-call history replay.

Legacy OpenClaw `2026.3.11` is rejected before the Gateway starts. A Chat
Completions provider is also rejected before the container starts. There is no
silent fallback.

Codex path requirements:

- Use Roller's `roller-codex:0.149.0` or a behavior-compatible newer image. The
  image must include Node.js, which starts the stdio MCP server injected with
  the rollout;
- Pass an absolute `--api-base` explicitly;
- Keep the default `--network host` and `--no-codex-websockets` so the container
  can reach the service through the rollout-local HTTP proxy. Configuration
  fails immediately when these are not met;
- Codex must enable the programmatic `exec` custom tool provided by the current
  image. Roller's default command already enables it. Users do not need to write
  a separate MCP config.

`--external-thinking` is a global setting for one collect run. In a dynamic
rollout, both the Task Agent and the Simulator inherit the mode. If the two
sides chose different models, both corresponding providers must satisfy the
protocol requirements above. Claude Code must still use
`--external-thinking off`.

## 3. Commands

### 3.1 OpenClaw

Force on the first turn, then on demand:

```bash
roller collect \
  --harness openclaw \
  --tasks tasks.jsonl \
  --model my-gpt-proxy/gpt-5.6-sol \
  --model-config examples/config/openclaw/models-reasoning.json \
  --external-thinking external \
  --timeout 900
```

Force a standalone THINK before every action:

```bash
roller collect \
  --harness openclaw \
  --tasks tasks.jsonl \
  --model my-gpt-proxy/gpt-5.6-sol \
  --model-config examples/config/openclaw/models-reasoning.json \
  --external-thinking external_always \
  --timeout 1200 \
  --wire-trace full
```

You do not need to pass `--thinking off`. Even if the CLI or task writes
`high/max`, actual native thinking in external mode is still fixed to `off`.
Run `info.json` keeps the requested configuration.
`runtime.external_thinking.native_reasoning` on the sample records the actual
value.

### 3.2 Codex

Codex does not use an OpenClaw model config:

```bash
export ROLLER_CODEX_KEY='...'

roller collect \
  --harness codex \
  --tasks tasks.jsonl \
  --model gpt-5.6-sol \
  --api-base http://your-api.example/v1 \
  --api-key-env ROLLER_CODEX_KEY \
  --external-thinking external_always \
  --wire-trace full \
  --timeout 1200
```

You do not need to register `deep_think` by hand in `--codex-config`. Roller
copies and registers its own minimal MCP server on every rollout. Even if you
also pass `--thinking high` or the task sets `xhigh`, an external-reasoning run
does not forward that grade to Codex, and it fixes
`reasoning.effort="none"` in the final Responses body.

### 3.3 Current verification baseline

As of 22 August 2026, this repository completed real end-to-end verification
with the following combination:

| Item | Verified value |
| --- | --- |
| Codex image | `roller-codex:0.149.0` / `codex-cli 0.149.0` |
| Model | `gpt-5.6-sol`, OpenAI-compatible Responses HTTP |
| `external` | `think → optional → ...`; sample protocol validation passed |
| `external_always` | `think → act → think → act`; 2 deep-think calls and 2 pairs passed |
| Training projection | ACTF keeps `deep_think`; SFT removes the protocol and writes `reasoning_content` |

This verifies the protocol combination among Roller, Codex CLI, and the
Responses service. It does not mean every endpoint with the same model name is
automatically compatible. After changing the Codex version or API
implementation, re-accept with a short task and `--wire-trace full` before
starting batch collection.

## 4. The two modes

### `external`

OpenClaw's first Responses request:

- tools include the canonical strict `deep_think` schema;
- `tool_choice={"type":"function","name":"deep_think"}`;
- `parallel_tool_calls=false`;
- uses external reasoning instructions.

Once history already contains a `deep_think` function call, later requests drop
the forced choice, keep the tool, and set `parallel_tool_calls=true`. The model
can combine think with multiple business tools, or finish directly. At the end,
Roller checks that the first assistant response called `deep_think` exactly
once, and that every thoughts value is non-empty.

Codex MCP tools are not sent as an ordinary top-level function schema. They are
exposed in programmatic custom `exec` as
`tools.mcp__roller_external_thinking__deep_think`. The first request keeps only
the `functions.exec` namespace and sets `tool_choice="required"`. Later requests
restore the full tool namespace and enter `optional` only after history contains
a successful call that returned `------`. Emitting the correct JavaScript while
the MCP execution actually failed is not treated as already having THOUGHT.

### `external_always`

Roller determines the phase from the actual history of this Responses `input`.
It does not use an in-memory toggle that HTTP retries can drift:

- Initial request, a new user message, or after a business tool output → THINK;
- Latest function call is `deep_think` and its output has been replayed → ACT.

An OpenClaw THINK request keeps every business tool schema for planning, but
forces and allows only one `deep_think`. An ACT request removes `deep_think`
from the tools that are actually forwarded, and allows business tools or a
final answer.

A Codex THINK request temporarily restricts `additional_tools` to the single
`functions.exec` and requires that exec to call the deep-think MCP once. An ACT
request restores Codex's original `additional_tools`, forbids deep-think, and
lets Codex run business tools or a final answer. Both paths derive the phase
from actual request history. They do not store an in-memory toggle that would
drift on HTTP retry, and they strictly validate THINK/ACT pairing at
finalization.

Compared with `external`, the number of model calls is close to 2×. `--timeout`
is not doubled automatically. Increase it yourself according to task length.

## 5. Implementation boundaries

```text
OpenClaw: package plugin -> OpenClaw local tool -> direct deep_think function call
Codex:    stdio MCP      -> programmatic exec -> nested deep_think MCP call
                                   │
                                   v
Roller wire proxy -> Responses request phase control -> Harness native raw
                  -> canonical ACTF validation -> SFT reasoning_content folding
```

Roller does not modify the official image, OpenClaw source, or Codex source.
Neither the OpenClaw plugin nor the Codex MCP server has file, shell, network,
or host privileges. They only accept non-empty thoughts and return fixed text.
The wire controller applies only to `/responses` requests of the current
rollout. It does not modify other API requests.

Compared with the referenced claw-eval implementation, current Roller keeps the
core request protocol and finalization checks, with two explicit differences:

- claw-eval retries THINK/ACT semantic violations up to twice each. Roller
  currently lets OpenClaw handle HTTP transport retries. A semantic violation
  marks the sample as error at transcript finalization and does not make extra
  model calls;
- claw-eval can use a placeholder only for the paired ACT after a THINK has
  consumed a large tool result above a threshold. Roller does not apply that
  temporary context rewrite. Actual context is still managed by OpenClaw
  compaction/pruning.

## 6. Output files

### `trace.jsonl`

OpenClaw keeps the direct `toolCall` and `toolResult`: thoughts are at
`message.content[].arguments.thoughts`, and the result is `------`.

Codex keeps its real native transport: the model response is a
`custom_tool_call` named `exec`, whose JavaScript input calls the MCP handle.
The MCP completion event stores the explicit reasoning. The common shape is
`event_msg.item_completed.item(type="McpToolCall").arguments.thoughts`; another
session-writer path records
`event_msg.mcp_tool_call_end.invocation.arguments.thoughts`. The following
`custom_tool_call_output` contains `------`. This is Codex's original fact. It
is not disguised as a top-level function call in raw.

### `trace_ACTF.json`

Both Harnesses are normalized to the same structure: `deep_think` is in
`assistant_content.tool_calls`, and the matching result is in `observation`.
The Codex converter accepts both native MCP completion events above and uses
the real server/tool/arguments to restore the outer `exec` wrapper to canonical
`deep_think`. Roller does not also copy it into `reasoning_content` in ACTF, to
avoid duplication.

### `trace_SFT.json` / `sft.jsonl`

SFT does not require the trained model to keep calling an internal scratchpad
tool. The converter applies this projection:

- Delete the `deep_think` call, the matching `------` tool result, the top-level
  schema, and the matching entry in the system-prompt tool list;
- Write thoughts from a standalone THINK response into the following ACT
  assistant's `reasoning_content`;
- If think and a business tool appear in the same assistant, write them into
  that assistant's `reasoning_content`;
- Keep every business tool call/result. If visible native reasoning already
  exists, join the segments in time order with a blank line.

The top level is still strictly only `messages` and `tools`. This
`reasoning_content` is a training projection, not wire evidence of a native
provider reasoning field. To inspect the real protocol you must look at raw or
full wire.

The Claude Code Harness does not currently implement external thinking.
`--harness claude-code` accepts only `--external-thinking off`, and exports the
plaintext thinking the service actually returned from the Claude native
session. Claude's optional `--claude-signature-recovery fill|prefer` is
provider-assisted signature replay after the task finishes. It does not inject a
`deep_think` tool and does not take part in the Agent THINK/ACT loop, so it is
not the external thinking described in this chapter. That restriction is
unrelated to calling the Anthropic Messages API or collecting its traces. See
[Claude Code Harness guide](../harnesses/claude-code-configuration.md).

### `wire_trace.jsonl`

`--wire-trace full` saves the body the proxy actually forwarded for both
OpenClaw and Codex when they support external thinking, and adds diagnostic
fields:

```json
{"external_thinking_phase":"think","body":{"reasoning":{"effort":"none"}}}
```

`external_always` should appear as `think/act/think/act` in order. `external`
is `think` first, then `optional`. A Codex THINK body keeps only programmatic
`functions.exec`; an ACT body restores the original `additional_tools`.
OpenClaw changes function tools directly. Request-only wire does not save the
response body.

### `info.json`

```json
{
  "runtime": {
    "external_thinking": {
      "mode": "external_always",
      "native_reasoning": "off",
      "validated": true,
      "deep_think_calls": 2,
      "thought_action_pairs": 2,
      "validation_error": null
    }
  }
}
```

## 7. How to inspect and accept

The Viewer reads ACTF, so it shows external reasoning as a `deep_think` tool
call, not as already-folded SFT reasoning:

```bash
roller view runs/examples_codex_external_always
```

On the page you should see the THINK step's `deep_think` arguments, the fixed
result `------`, and then a separate business-tool or final step. To check the
training record, inspect `trace_SFT.json` directly:

```bash
sample=runs/examples_codex_external_always/samples/<model>/<task>
jq '.runtime.external_thinking' "$sample/info.json"
jq '[.steps[].assistant_content.tool_calls[]?.name]' "$sample/trace_ACTF.json"
jq '[.messages[] | select(.role == "assistant") |
    {reasoning_content, content, tool_calls}]' "$sample/trace_SFT.json"
```

With `--wire-trace full`, you can also check the phase that was actually
forwarded:

```bash
jq -c '{seq, phase: .external_thinking_phase,
        effort: .body.reasoning.effort,
        tool_choice: .body.tool_choice}' "$sample/wire_trace.jsonl"
```

A successful `external_always` should start at `think` and strictly alternate
to `act`. Every request's effort should be `none`. A successful `external` is
`think` first, then `optional`.

## 8. Common errors

### `Codex external thinking requires --api-base`

External thinking must control the protocol at the final Responses HTTP
boundary, so it cannot rely on Codex's built-in provider alone. Pass an
absolute `http://.../v1` or `https://.../v1`.

### Requires `--network host` or `--no-codex-websockets`

The Codex container needs to reach the rollout-local HTTP proxy on the host.
Keep the default network and disable Responses WebSocket. Both conditions are
validated before the container starts.

### Missing `programmatic custom exec`

Codex did not expose the custom `exec` that Roller has verified in Responses
`additional_tools`. Use the pinned `roller-codex:0.149.0`, or re-verify
programmatic tool calling after upgrading the image. There is no silent
fallback.

### `external_always ... unpaired THINK` or another `deep_think` in the ACT phase

The model did not follow the strict alternating protocol. The sample keeps
raw/ACTF for diagnosis, but its status is set to `error` and it does not enter
run-level `sft.jsonl`. Check the full-wire phase, instructions, and whether the
API fully replayed custom tool call/output history.

### `deep_think ... expected placeholder`

The MCP call failed or was replaced, or custom `exec` did not hand the MCP
return value to `text(...)`. Roller treats the phase as complete only when it
sees a successful `------` result, so failed JavaScript is not counted as THINK.

## 9. Safety and training notes

- Thoughts are fully visible model output. They may contain sensitive inputs,
  credentials, personal information, or incorrect inferences. Manage them as
  training data, not as ordinary logs.
- `--no-keep-thinking` does not delete external thoughts. They remain tool
  arguments in raw/ACTF and are still projected to `reasoning_content` in SFT.
  To stop saving explicit CoT, you must use `--external-thinking off`.
- `external_always` increases tokens, latency, and API request count. When
  comparing model cost, do not mix it with native mode without labeling the
  protocol.
- SFT has already removed the `deep_think` protocol and does not require that
  tool at deploy time. The training framework must support assistant
  `reasoning_content`, or you must convert it to the target framework's
  scratchpad representation during preprocessing.
