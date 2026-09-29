# OpenClaw thinking 档位

本页回答两个问题：`--thinking` 应该填什么，以及这个值最终会变成什么请求参数。
基础配置和轨迹保留规则见 [OpenClaw 配置](openclaw-configuration.md)。

## 1. 先选 provider，再选档位

档位经过三层处理：Roller 接收逻辑值 → OpenClaw 按 provider/model/runtime 校验和映射 → API 服务执行。
API 接受 `max`，不等于任意 OpenClaw provider 都能用 `--thinking max`。

Roller 接受 `off/minimal/low/medium/high/xhigh/adaptive/max/ultra`；`none` 是 `off` 的别名。
task 的 `runtime.thinking` 优先于 CLI。未设置时不注入档位，使用 OpenClaw 自身的默认策略。
`--no-keep-thinking` 只控制轨迹和后续请求中的历史 reasoning 保留，不关闭模型推理。

以下 OpenClaw 行为核对自镜像 `ghcr.io/openclaw/openclaw:2026.7.1-2`（CLI 报告 `2026.7.1`）。
API 结果来自 2026-09-18 对一套兼容服务的实测，只适用于列出的模型 ID 和协议，不是所有同系列模型的承诺。
升级镜像后应重新检查；[OpenClaw 官方说明](https://docs.openclaw.ai/tools/thinking) 也会随版本更新。

## 2. 支持表

| 模型与接入方式 | OpenClaw 逻辑档位 / 建议 | 已验证的 API 行为与限制 |
| --- | --- | --- |
| `openai/gpt-5.6-sol`，`openai-responses`，`agentRuntime.id: openclaw` | profile 接受 `off/minimal/low/medium/high/xhigh/max/ultra`；通常选 `low/medium/high/xhigh/max`，不支持 `adaptive` | Responses 接受 `none/low/medium/high/xhigh/max`；原生 `minimal/adaptive/ultra` 返回 400。逻辑 `minimal` 映射为 `low`。`ultra` 是 OpenClaw 策略，不是 API effort；见下文 |
| 自定义 provider 下的 `gpt-5.6-sol` | 通用 profile 默认只到 `high`；声明 `xhigh` 可扩展到 `xhigh`，不能据此开启 `max/ultra` | 同一个 API 模型也会因 provider 名不同而被 OpenClaw 提前拒绝。GPT 推荐上一行的配置 |
| `glm-5.3-flash`，自定义 `openai-completions` | 使用 `low` 或 `high`；不要统一设置 `medium` | 服务接受 `low/high/max`；`medium/off/none` 返回 400，模型始终思考。通用 OpenClaw profile 不开放 `max`，即使后端接受 |
| `kimi-k3`，自定义 `openai-completions` | 本轮验证 `medium/high`；声明支持 `none` 仍不能保证 `--thinking off` 关闭推理，见下文 | 服务接受 `none/medium/high`，`off` 返回 400。开启 thinking 时 `max_completion_tokens` 必须大于服务的 `thinking_budget`（本次为 32768）；不能照搬小输出预算 |
| `bailian/deepseek-v4.1-flash`，`openai-completions` | 通用 profile 可用 `medium`；不要把 API 的 `max` 支持当成 CLI 的 `max` 支持 | 服务接受省略 effort、`none/medium/max`。接入兼容端点时用其声明的字段，不套用原生 DeepSeek 的 `thinking` 扩展 |
| `qwen3.8-flash`，Qwen-compatible Chat | `compat.thinkingFormat: qwen` 选择布尔参数；此镜像中 `--thinking off` 仍发送开启，见下文 | 服务接受 `enable_thinking: false/true`，但 OpenClaw Agent 链路实测 `off/low` 都发送 `true`，不能据此区分 `low/medium/high` 的强度 |
| `claude-opus-4-8`，`anthropic-messages` | profile 接受 `off/minimal/low/medium/adaptive/high/xhigh/max`；不支持 `ultra` | 服务接受 disabled，以及 adaptive thinking 配合 `output_config.effort: low/medium/high/xhigh/max`。简单题只返回 text；本轮未验证 `claude-opus-4-8-thinking` 别名成功 |

表中的“API 接受”表示请求成功，不证明后端确实区分了每个档位，也不等于整条 Roller rollout/SFT 导出已通过。
OpenClaw 的通用 profile 不是后端能力探测器：`reasoning: true` 时通常提供
`off/minimal/low/medium/high`，即使服务只支持其中一部分。需要按服务填写兼容配置和选择档位。

### GPT 的 `ultra` 和 `off`

- 在上述 GPT/OpenClaw runtime 组合中，`ultra` 使用 provider 的 `max` 并启用 OpenClaw 的额外编排策略，不向 API 发送 `effort: ultra`。CLI 帮助未列出它，不代表 profile 不支持它。Roller 不再按 `2026.7.1` 版本号一律拦截；是否可用由 OpenClaw 校验。本轮没有完成 `ultra` 的真实编排验收。
- `off` **不能在所有路由上保证关掉 GPT 推理**。该版本 OpenClaw 对非原生 OpenAI 地址会删除 `reasoning.effort: none`。Roller 的本地 wire proxy 也会改变 OpenClaw 看到的地址。真实 rollout 已观察到 `--thinking off` 的请求不含 `reasoning`；直连测试中，此服务省略 effort 后使用 `medium`。应检查实际请求，不能只看命令参数。

### Chat Completions 的 `off` 限制

关闭推理的限制也影响此镜像中的 Qwen 和通用 Chat 模型。使用原版镜像、Roller 和本地模拟
Chat Completions 服务的完整链路检查得到：

| 模型配置 | 逻辑档位 | 实际请求 |
| --- | --- | --- |
| Qwen，`compat.thinkingFormat: qwen` | `off` | `enable_thinking: true` |
| Kimi，`compat.supportedReasoningEfforts: ["none", "medium", "high"]` | `off` | `reasoning_effort: high` |

两条任务的原生 session 都记录了 `thinkingLevel: off`，并成功导出 ACTF/SFT。
OpenClaw Agent 在这些配置下把 `off` 转成缺失的 reasoning 选项，Chat adapter 随后使用默认 `high`，
Qwen 的布尔参数因此变成 `true`。声明 `none` 或 `thinkingFormat: qwen` 不能绕过这个行为。
仅向底层 transport 直接传入 `off` 的测试会跳过该 Agent 步骤，不能证明 Roller 链路能够关闭推理。

这些检查验证的是实际请求参数和模拟响应的导出，不代表真实模型的推理行为或 CoT。
需要关闭推理时，应先核对完整链路的 wire 请求确实包含服务接受的关闭值，再进行批量采集或开关对照实验。

## 3. 配置示例

### GPT：使用 OpenAI profile

[`models-reasoning.json`](../../../examples/config/openclaw/models-reasoning.json) 将 GPT provider 命名为 `openai`，协议为 `openai-responses`，并显式设置 `agentRuntime: {"id": "openclaw"}`。
`baseUrl` 仍可指向兼容服务；provider 名用于选择 OpenClaw 策略，不要求改成官方 API 地址。

```bash
roller collect --harness openclaw \
  --tasks examples/tasks/tasks.jsonl \
  --model openai/gpt-5.6-sol \
  --model-config examples/config/openclaw/models-reasoning.json \
  --thinking high --wire-trace full --limit 1 --output ./runs
```

运行前设置模板中的 `MY_PROXY_BASE_URL/MY_PROXY_API_KEY`；删除不用的 `my-chat-proxy` provider，或同时设置其 `MY_CHAT_BASE_URL/MY_CHAT_API_KEY`。所有占位符都会被展开。
从旧模板迁移时，GPT model ref 从 `my-gpt-proxy/gpt-5.6-sol` 改为 `openai/gpt-5.6-sol`；自己的已有配置不会被自动改名。

### Chat Completions：声明服务支持的字段和值

以下是 model 对象的片段，不是完整 provider 配置。以计划使用 `low/high` 的 GLM 服务为例：

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

配合 `--thinking low` 或 `high` 使用。若确实要把逻辑 `medium` 转成服务的 `high`，在 `compat` 中加
`"reasoningEffortMap": {"medium": "high"}`；这会改变含义，不建议对所有模型统一设置。

- `supportsReasoningEffort: false` 让此 adapter 不发送 `reasoning_effort`，不是关闭后端 thinking。
- 仅在服务确实接受时，将 `none` 加入 `supportedReasoningEfforts`。这只声明 API 能力，不保证此镜像的 Agent 能把 `off` 传给 adapter。始终思考的模型不应这样声明。
- Qwen 的布尔参数使用 `compat: {"thinkingFormat": "qwen"}`。此配置不能修复上述 `off` 限制。服务不接受 `enable_thinking` 时不要使用。
- 对仅支持 OpenAI 字段的 DeepSeek 兼容端点，可显式使用 `compat.thinkingFormat: openai`；原生 DeepSeek 字段策略不适用于所有代理。
- 此镜像的 schema 接受 `compat.supportedReasoningEfforts` 和 `compat.reasoningEffortMap`。旧文档称它们不受支持有误。顶层 `thinkingLevelMap` 不能替代 Chat adapter 的 `reasoningEffortMap`，也不会自动为通用 profile 开启 `max`。

模型显式声明的 `reasoning: false` 会保留，不会因 `--thinking high` 被 Roller 改成 `true`。
缺失该字段时，Roller 才为显式启用的 thinking 补充 `true`；支持范围最终仍由 OpenClaw 和服务决定。

## 4. 怎样验收

先用一个短任务和单一模型验收，再批量采集。建议 `--wire-trace full`，确认：

1. `info.json` 中的实际 OpenClaw 版本、状态和错误；不要只看外层 collect 命令退出码。
2. `wire_trace.jsonl` 中真实请求的 `reasoning.effort`、`reasoning_effort`、`thinking`、`enable_thinking` 或 `output_config.effort`。没有请求时，先检查 profile/配置校验错误。
3. 返回的可见 reasoning，以及 ACTF/SFT 是否成功导出。`reasoning_tokens > 0` 不保证有可见文本，GPT summary 和 Claude text-only 回复均不能据此判错。

wire 文件含请求上下文，应按敏感数据保管。格式及原生记录与 wire 的区别见 [轨迹产物](../traces/trajectory-outputs.md)。
本轮真实 Roller 验收中，`openai/gpt-5.6-sol --thinking max` 和自定义 provider 的
`glm-5.3-flash --thinking low` 均完成短任务并成功导出 SFT；wire 分别记录
`reasoning.effort: max` 和 `reasoning_effort: low`。这两条简单算术任务的 SFT 未包含可见 reasoning 文本。

本集成不通过修改镜像源码或在代理中补写 effort 来绕过 OpenClaw 的能力判断。GPT 代理路由和上述 Chat Agent 链路的 `off` 行为仍是已知限制。
