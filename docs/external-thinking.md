# External Thinking：以工具调用保存显式 CoT

参考https://github.com/can1357/oh-my-pi， Roller 实现了类似的 `external` 与 `external_always` 模式：关闭 provider 原生 reasoning，
把模型明确生成的中间推理保存进 `deep_think` 工具参数，以及如何在专用 SFT 中把它
标准化为 `reasoning_content`。

## 1. 为什么需要它

OpenAI Responses 的 reasoning item 可以包含 reasoning token、opaque/encrypted state 和可见
summary，但客户端拿到的可见文本通常是压缩摘要。Roller 的 native `--thinking` 会忠实保存
该 summary，不会把隐藏 token 反推出完整 CoT。

External thinking 采用另一种协议：关闭 native reasoning，让模型把愿意暴露并供训练的
推理文本写入普通 function call：

```text
assistant -> deep_think({"thoughts": "..."})
tool      -> ------
assistant -> business tool call or final answer
```

这段 thoughts 是模型显式输出，不是对 OpenAI 隐藏 reasoning 的破解或恢复。

## 2. 使用条件

- 默认官方镜像 `ghcr.io/openclaw/openclaw:2026.7.1-2`，或兼容其 plugin contract 的更新版；
- `--model-config` 中所选 provider 使用 `"api": "openai-responses"`；
- provider 有绝对 `baseUrl`，因为 Roller 必须在最终 HTTP 边界执行协议控制；
- 模型服务支持 Responses function tools、named `tool_choice` 和 function-call history replay。

legacy OpenClaw `2026.3.11` 会在 Gateway 启动前被拒绝。Chat Completions provider 也会在
容器启动前被拒绝，不做静默降级。

`--external-thinking` 是一次 collect run 的全局设置。User Simulator rollout 中，task
Agent 与 Simulator 都会继承该模式；如果双方选择了不同模型，则两个 provider 都必须满足
上述 Responses 条件。只希望 task Agent 使用 external thinking 时，应把 Simulator 拆成独立
run，或保持该次 run 为 `off`。

## 3. 命令

首轮强制、后续按需：

```bash
roller collect \
  --tasks tasks.jsonl \
  --model my-gpt-proxy/gpt-5.6-sol \
  --model-config examples/json/openclaw-models-reasoning.json \
  --external-thinking external \
  --timeout 900
```

每个动作前强制独立 THINK：

```bash
roller collect \
  --tasks tasks.jsonl \
  --model my-gpt-proxy/gpt-5.6-sol \
  --model-config examples/json/openclaw-models-reasoning.json \
  --external-thinking external_always \
  --timeout 1200 \
  --wire-trace full
```

不需要再传 `--thinking off`。即使 CLI 或 task 写了 `high/max`，external 模式的实际 native
thinking 仍固定为 `off`；run `info.json` 保留请求配置，sample
`runtime.external_thinking.native_reasoning` 明确记录实际值。

## 4. 两种模式

### `external`

首次 Responses 请求：

- tools 中包含 canonical strict `deep_think` schema；
- `tool_choice={"type":"function","name":"deep_think"}`；
- `parallel_tool_calls=false`；
- 使用 external reasoning instructions。

一旦 history 中已有 `deep_think` function call，后续请求取消强制选择，保留该工具并设置
`parallel_tool_calls=true`。模型可以把 think 与多个业务工具组合，也可以直接完成。结束后
Roller 校验第一条 assistant response 恰好调用一次 `deep_think`，并校验所有 thoughts 非空。

### `external_always`

Roller 根据本次 Responses `input` 的实际历史确定阶段，不使用易受 HTTP 重试影响的内存
toggle：

- 初始请求、新 user message、或 business tool output 之后进入 THINK；
- 最新 function call 为 `deep_think` 且其 output 已回放时进入 ACT。

THINK 请求保留所有 business tool schema 供规划，但强制且只允许执行一个
`deep_think`。ACT 请求从实际转发的 tools 中删除 `deep_think`，允许并行 business tools
或 final answer。raw 收尾校验 assistant messages 严格按 THINK/ACT 成对交替。

相比 `external`，模型调用数接近两倍；`--timeout` 不会自动翻倍，请按任务长度主动增加。

## 5. 实现边界

```text
Roller package plugin
  -> 复制到一次性容器
  -> OpenClaw 注册 deep_think 本地工具
  -> Roller wire proxy 改写 Responses request
  -> provider 返回 function_call
  -> OpenClaw 执行本地 no-op，返回 ------
  -> transcript SDK 导出 raw
  -> Roller 校验 external 协议并生成 ACTF/SFT
```

Roller 不修改官方镜像或镜像内 OpenClaw 源码。插件没有文件、shell、网络或宿主权限；其
execute handler只返回固定文本。wire 控制器只作用于当前 rollout 的 `/responses` 请求，
不会修改其他 API 请求。

与所参考 claw-eval 实现相比，当前 Roller 保留核心请求协议与收尾校验，但有两点明确差异：

- claw-eval 会对 THINK/ACT 语义违规各重试最多两次；Roller 当前让 OpenClaw 处理 HTTP
  transport retry，语义违规在 transcript 收尾时把 sample 标为 error，不额外调用模型；
- claw-eval 可在 THINK 已消费超过阈值的大 tool result 后，仅对配对 ACT 使用 placeholder；
  Roller 不做这层临时 context 改写，实际 context 仍由 OpenClaw 的 compaction/pruning 管理。

## 6. 产出文件

### `trace.jsonl`

保留真实的 `toolCall` 与 `toolResult`：thoughts 位于
`message.content[].arguments.thoughts`，结果为 `------`。

### `trace_ACTF.json`

`deep_think` 与业务工具一样位于 `assistant_content.tool_calls`，对应结果位于
`observation`。Roller 不把它复制到 `reasoning_content`，避免同一文本在两种协议字段重复。

### `trace_SFT.json` / `sft.jsonl`

SFT 不要求训练后的模型继续调用内部 scratchpad 工具。转换器执行以下投影：

- 删除 `deep_think` call、对应的 `------` tool result、顶层 schema 和 OpenClaw system
  prompt 工具清单中的对应条目；
- 将独立 THINK response 的 thoughts 写入随后 ACT assistant 的 `reasoning_content`；
- 若 think 与业务工具出现在同一个 assistant，则写入该 assistant 的
  `reasoning_content`；
- 保留所有业务 tool call/result；若已有可见 native reasoning，则按时间顺序以空行连接。

顶层仍严格只有 `messages` 与 `tools`。该 `reasoning_content` 是训练投影，不是 provider
原生 reasoning 字段的 wire 证据；检查真实协议必须查看 raw 或 full wire。

### `wire_trace.jsonl`

`--wire-trace full` 保存实际转发 body，并增加诊断字段：

```json
{"external_thinking_phase":"think","body":{"reasoning":{"effort":"none"}}}
```

`external_always` 应依次出现 `think/act/think/act`；`external` 首次为 `think`，之后为
`optional`。request-only wire 不保存响应 body。

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

## 7. 安全与训练注意事项

- thoughts 是完整可见的模型输出，可能包含敏感输入、凭证、个人信息或错误推断；按训练
  数据而不是普通日志管理。
- `--no-keep-thinking` 不删除 external thoughts；它们在 raw/ACTF 中仍是工具参数，在 SFT
  中仍投影为 `reasoning_content`。要禁止保存显式 CoT，必须使用
  `--external-thinking off`。
- `external_always` 会增加 token、延迟和 API 请求数；比较模型成本时不要和 native 模式
  直接混用而不标注协议。
- SFT 已移除 `deep_think` 协议，不要求部署时提供该工具；训练框架必须支持 assistant
  `reasoning_content`，或在数据预处理阶段把它转换成目标框架的 scratchpad 表示。
