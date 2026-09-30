# OpenClaw thinking levels

This page explains which `--thinking` value to choose and what reaches the model API.
See [OpenClaw configuration](openclaw-configuration.md) for basic setup and trace retention.

## 1. Choose the provider before the level

There are three layers: Roller accepts a logical level → OpenClaw validates and maps it for the
provider/model/runtime → the API service executes it. An API accepting `max` does not mean every
OpenClaw provider accepts `--thinking max`.

Roller accepts `off/minimal/low/medium/high/xhigh/adaptive/max/ultra`; `none` aliases `off`.
Task `runtime.thinking` overrides the CLI. When unset, Roller injects no level and leaves defaults to OpenClaw.
`--no-keep-thinking` controls historical reasoning in traces and subsequent requests; it does not disable model reasoning.

The OpenClaw behavior below was checked in `ghcr.io/openclaw/openclaw:2026.7.1-2` (CLI version `2026.7.1`).
API results are from one compatible service tested on 2026-09-18, for the exact IDs and protocols listed.
They are not guarantees for every model in a family. Recheck after image upgrades;
[OpenClaw's official documentation](https://docs.openclaw.ai/tools/thinking) also changes with releases.

## 2. Support table

| Model and adapter | OpenClaw logical levels / recommendation | Verified API behavior and limits |
| --- | --- | --- |
| `openai/gpt-5.6-sol`, `openai-responses`, `agentRuntime.id: openclaw` | Profile accepts `off/minimal/low/medium/high/xhigh/max/ultra`; normally choose `low/medium/high/xhigh/max`. No `adaptive` | Responses accepts `none/low/medium/high/xhigh/max`; native `minimal/adaptive/ultra` return 400. Logical `minimal` maps to `low`. `ultra` is an OpenClaw policy, not an API effort; see below |
| `gpt-5.6-sol` under a custom provider | Generic profile goes through `high`; declaring `xhigh` adds that level, not `max/ultra` | OpenClaw can reject the same API model based on its provider name. Prefer the GPT configuration above |
| `glm-5.3-flash`, custom `openai-completions` | Use `low` or `high`, not a blanket `medium` | Service accepts `low/high/max`; `medium/off/none` return 400 because this model always thinks. Generic OpenClaw profile does not expose `max` even though the API accepts it |
| `kimi-k3`, custom `openai-completions` | `medium/high` tested; declaring `none` support still does not guarantee that `--thinking off` disables reasoning; see below | Service accepts `none/medium/high`; `off` returns 400. With thinking on, `max_completion_tokens` must exceed this service's `thinking_budget` (32768 here); do not copy small output limits |
| `bailian/deepseek-v4.1-flash`, `openai-completions` | Generic profile can use `medium`; API `max` support does not imply CLI `max` support | Service accepts omitted effort and `none/medium/max`. Use the compatible endpoint's fields, not native DeepSeek `thinking` extensions without checking support |
| `qwen3.8-flash`, Qwen-compatible Chat | `compat.thinkingFormat: qwen` selects a boolean parameter; this image still sends enabled thinking for `--thinking off`; see below | Service accepts `enable_thinking: false/true`, but the OpenClaw Agent path sent `true` for both `off/low`; this does not establish distinct `low/medium/high` intensities |
| `claude-opus-4-8`, `anthropic-messages` | Profile accepts `off/minimal/low/medium/adaptive/high/xhigh/max`; no `ultra` | Service accepts disabled thinking, and adaptive thinking with `output_config.effort: low/medium/high/xhigh/max`. Simple prompts returned only text. The `claude-opus-4-8-thinking` alias was not successfully verified in this round |

“API accepts” means a successful request, not proof that every level changes backend behavior or that
a complete Roller rollout and SFT export passed. OpenClaw's generic profile is not service discovery:
with `reasoning: true`, it normally offers `off/minimal/low/medium/high` even if the service supports only
a subset. Configure compatibility and choose levels for the actual endpoint.

### GPT `ultra` and `off`

- In the GPT/OpenClaw runtime combination above, `ultra` uses provider `max` with additional OpenClaw orchestration, not `effort: ultra`. Its absence from CLI help does not mean the profile rejects it. Roller no longer blocks it solely on version `2026.7.1`; OpenClaw validates support. A real `ultra` orchestration run was not completed in this check.
- `off` **does not guarantee disabled GPT reasoning on every route**. This OpenClaw version strips `reasoning.effort: none` for non-native OpenAI URLs. Roller's local wire proxy also changes the URL seen by OpenClaw. A real rollout with `--thinking off` sent no `reasoning` field; a direct request without effort used this service's `medium` default. Inspect the wire request, not just the command line.

### Chat Completions `off` limitations

Disabling reasoning is also affected for Qwen and generic Chat models in this image. End-to-end checks
using the unmodified image, Roller, and a local mock Chat Completions service produced:

| Model configuration | Logical level | Actual request |
| --- | --- | --- |
| Qwen, `compat.thinkingFormat: qwen` | `off` | `enable_thinking: true` |
| Kimi, `compat.supportedReasoningEfforts: ["none", "medium", "high"]` | `off` | `reasoning_effort: high` |

Both tasks recorded `thinkingLevel: off` in their native sessions and exported ACTF/SFT successfully.
For these configurations, the OpenClaw Agent converts `off` to an absent reasoning option. The Chat
adapter then defaults to `high`, which also makes Qwen's boolean parameter `true`. Declaring `none`
or `thinkingFormat: qwen` does not bypass this behavior. Passing `off` directly to the low-level
transport skips that Agent step and cannot establish that the Roller path disables reasoning.

These checks verify request parameters and export of mock responses, not real-model reasoning behavior
or CoT. Before batch collection or comparing thinking on/off, check that the complete path's wire request
actually contains the disabled value accepted by your service.

## 3. Configuration

### GPT: use the OpenAI profile

[`models-reasoning.json`](../../../examples/config/openclaw/models-reasoning.json) names the GPT provider `openai`,
uses `openai-responses`, and explicitly selects `agentRuntime: {"id": "openclaw"}`.
`baseUrl` can still point to a compatible service. The provider name selects OpenClaw policy;
it does not require the official API endpoint.

```bash
roller collect --harness openclaw \
  --tasks examples/tasks/tasks.jsonl \
  --model openai/gpt-5.6-sol \
  --model-config examples/config/openclaw/models-reasoning.json \
  --thinking high --wire-trace full --limit 1 --output ./runs
```

Set `MY_PROXY_BASE_URL/MY_PROXY_API_KEY` first. Remove the unused `my-chat-proxy` provider or also set
`MY_CHAT_BASE_URL/MY_CHAT_API_KEY`: all placeholders are expanded.
When migrating from the old template, change the GPT model ref from `my-gpt-proxy/gpt-5.6-sol` to
`openai/gpt-5.6-sol`. Existing user configurations are not renamed automatically.

### Chat Completions: declare supported fields and values

This is a model fragment, not a complete provider config. For a service where you intend to use only `low/high`:

```json
{
  "id": "glm-5.3-flash",
  "name": "GLM",
  "reasoning": true,
  "compat": {
    "supportsReasoningEffort": true,
    "supportedReasoningEfforts": ["low", "high"]
  }
}
```

Use `--thinking low` or `high`. To deliberately map logical `medium` to API `high`, add
`"reasoningEffortMap": {"medium": "high"}` inside `compat`. This changes semantics; do not apply it to all models.

- `supportsReasoningEffort: false` prevents this adapter from sending `reasoning_effort`; it does not turn off backend thinking.
- Add `none` to `supportedReasoningEfforts` only if the service accepts it. This declares API capability but does not guarantee that this image's Agent forwards `off` to the adapter. Do not declare it for always-thinking models.
- Qwen's boolean parameter uses `compat: {"thinkingFormat": "qwen"}`. This setting does not fix the `off` limitation above. Do not use it on endpoints that reject `enable_thinking`.
- For DeepSeek-compatible endpoints accepting only OpenAI fields, explicitly use `compat.thinkingFormat: openai`. Native DeepSeek field policies do not apply to all proxies.
- This image's schema accepts `compat.supportedReasoningEfforts` and `compat.reasoningEffortMap`; the previous documentation said otherwise incorrectly. Top-level `thinkingLevelMap` is not a substitute for the Chat adapter's `reasoningEffortMap`, nor does it enable `max` in the generic profile.

Roller preserves an explicit `reasoning: false` even when `--thinking high` is requested. Only missing
metadata is filled with `true` when thinking is explicitly enabled. OpenClaw and the service still decide support.

## 4. Verify a configuration

Start with one short task and model before batch collection. Use `--wire-trace full` and check:

1. Actual OpenClaw version, status, and errors in `info.json`, not just the outer collect command's exit code.
2. Actual `reasoning.effort`, `reasoning_effort`, `thinking`, `enable_thinking`, or `output_config.effort` in `wire_trace.jsonl`. If there is no request, check profile/config validation first.
3. Visible reasoning in responses and successful ACTF/SFT export. `reasoning_tokens > 0` does not guarantee visible text; a GPT summary or Claude text-only response is not itself an error.

Wire files contain request context; treat them as sensitive data. See [Trajectory outputs](../traces/trajectory-outputs.md)
for file formats and native-vs-wire differences.

In real Roller checks, `openai/gpt-5.6-sol --thinking max` and custom-provider
`glm-5.3-flash --thinking low` both completed short tasks and exported SFT successfully. Wire requests
contained `reasoning.effort: max` and `reasoning_effort: low`, respectively. Neither simple arithmetic
task's SFT contained visible reasoning text.

This integration does not patch image sources or inject effort in the proxy to bypass OpenClaw's
capability checks. The GPT proxy-route and Chat Agent `off` behaviors above remain known limitations.
