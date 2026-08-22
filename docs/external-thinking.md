# External Thinking：以工具调用保存显式 CoT

参考 https://github.com/can1357/oh-my-pi，Roller 为 OpenClaw 和 Codex 实现了类似的
`external` 与 `external_always` 模式：关闭 provider 原生 reasoning，把模型明确生成的
中间推理保存进 `deep_think` 工具参数，再在专用 SFT 中标准化为
`reasoning_content`。

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

共同要求：模型服务必须实现 Codex/OpenClaw 实际使用的 OpenAI Responses streaming、
tool choice 和 tool history replay。只有 `/v1/chat/completions` 可用还不够。

OpenClaw 路径要求：

- 默认官方镜像 `ghcr.io/openclaw/openclaw:2026.7.1-2`，或兼容其 plugin contract 的更新版；
- `--model-config` 中所选 provider 使用 `"api": "openai-responses"`；
- provider 有绝对 `baseUrl`，因为 Roller 必须在最终 HTTP 边界执行协议控制；
- 模型服务支持 Responses function tools、named `tool_choice` 和 function-call history replay。

legacy OpenClaw `2026.3.11` 会在 Gateway 启动前被拒绝。Chat Completions provider 也会在
容器启动前被拒绝，不做静默降级。

Codex 路径要求：

- 使用 Roller 的 `roller-codex:0.149.0` 或行为兼容的更新镜像；镜像中必须有 Node.js，
  用于启动随 rollout 注入的 stdio MCP server；
- 明确传入绝对 `--api-base`；
- 保持默认 `--network host` 和 `--no-codex-websockets`，使容器能通过 rollout-local HTTP
  代理访问服务；不满足时配置阶段直接报错；
- Codex 必须启用当前镜像提供的 programmatic `exec` custom tool。Roller 的默认命令已启用，
  无需用户另写 MCP 配置。

`--external-thinking` 是一次 collect run 的全局设置。OpenClaw User Simulator rollout 中，
task Agent 与 Simulator 都会继承该模式；如果双方选择了不同模型，则两个 provider 都必须
满足上述 Responses 条件。Codex 当前不支持 User Simulator task。

## 3. 命令

### 3.1 OpenClaw

首轮强制、后续按需：

```bash
roller collect \
  --harness openclaw \
  --tasks tasks.jsonl \
  --model my-gpt-proxy/gpt-5.6-sol \
  --model-config examples/json/openclaw-models-reasoning.json \
  --external-thinking external \
  --timeout 900
```

每个动作前强制独立 THINK：

```bash
roller collect \
  --harness openclaw \
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

### 3.2 Codex

Codex 不使用 OpenClaw model config：

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

不需要在 `--codex-config` 中手工注册 `deep_think`；Roller 每个 rollout 都会复制并注册自己的
最小 MCP server。即使同时传了 `--thinking high` 或 task 配了 `xhigh`，外部推理运行也不会
把该档位传给 Codex，并在最终 Responses body 中固定 `reasoning.effort="none"`。

### 3.3 当前验证基线

截至 2026 年 8 月 22 日，本仓库使用以下组合完成了真实端到端验证：

| 项目 | 验证值 |
| --- | --- |
| Codex 镜像 | `roller-codex:0.149.0` / `codex-cli 0.149.0` |
| 模型 | `gpt-5.6-sol`，OpenAI-compatible Responses HTTP |
| `external` | `think → optional → ...`；sample 协议校验通过 |
| `external_always` | `think → act → think → act`；2 个 deep-think 与 2 个配对均通过 |
| 训练投影 | ACTF 保留 `deep_think`；SFT 删除协议并写入 `reasoning_content` |

这里验证的是 Roller、Codex CLI 和 Responses 服务之间的协议组合，不代表任意同名模型
endpoint 都自动兼容。更换 Codex 版本或 API 实现后，应先用短 task 和 `--wire-trace full`
重新验收，再开始批量采集。

## 4. 两种模式

### `external`

OpenClaw 的首次 Responses 请求：

- tools 中包含 canonical strict `deep_think` schema；
- `tool_choice={"type":"function","name":"deep_think"}`；
- `parallel_tool_calls=false`；
- 使用 external reasoning instructions。

一旦 history 中已有 `deep_think` function call，后续请求取消强制选择，保留该工具并设置
`parallel_tool_calls=true`。模型可以把 think 与多个业务工具组合，也可以直接完成。结束后
Roller 校验第一条 assistant response 恰好调用一次 `deep_think`，并校验所有 thoughts 非空。

Codex 的 MCP 工具不会作为普通顶层 function schema 发送，而是在 programmatic custom
`exec` 中以 `tools.mcp__roller_external_thinking__deep_think` 暴露。首次请求只保留
`functions.exec` namespace 并设置 `tool_choice="required"`；只有 history 中出现返回
`------` 的成功调用后，后续请求才恢复完整工具 namespace 并进入 `optional`。只输出正确的
JavaScript、但 MCP 实际执行失败，不会被误判为已经 THINK。

### `external_always`

Roller 根据本次 Responses `input` 的实际历史确定阶段，不使用易受 HTTP 重试影响的内存
toggle：

- 初始请求、新 user message、或 business tool output 之后进入 THINK；
- 最新 function call 为 `deep_think` 且其 output 已回放时进入 ACT。

OpenClaw THINK 请求保留所有 business tool schema 供规划，但强制且只允许执行一个
`deep_think`。ACT 请求从实际转发的 tools 中删除 `deep_think`，允许 business tools 或
final answer。

Codex THINK 请求把 `additional_tools` 临时限制为唯一的 `functions.exec`，并要求该 exec
只调用一次 deep-think MCP；ACT 请求恢复 Codex 原始 `additional_tools`，禁止 deep-think，
让 Codex 执行业务工具或 final answer。两条路径都从实际 request history 推导阶段，不保存
会在 HTTP retry 时漂移的内存 toggle，并在收尾时严格校验 THINK/ACT 成对交替。

相比 `external`，模型调用数接近两倍；`--timeout` 不会自动翻倍，请按任务长度主动增加。

## 5. 实现边界

```text
OpenClaw: package plugin -> OpenClaw local tool -> direct deep_think function call
Codex:    stdio MCP      -> programmatic exec -> nested deep_think MCP call
                                   │
                                   v
Roller wire proxy -> Responses request phase control -> Harness native raw
                  -> canonical ACTF validation -> SFT reasoning_content folding
```

Roller 不修改官方镜像、OpenClaw 源码或 Codex 源码。OpenClaw plugin 和 Codex MCP server
都没有文件、shell、网络或宿主权限，只接收非空 thoughts 并返回固定文本。wire 控制器只
作用于当前 rollout 的 `/responses` 请求，不会修改其他 API 请求。

与所参考 claw-eval 实现相比，当前 Roller 保留核心请求协议与收尾校验，但有两点明确差异：

- claw-eval 会对 THINK/ACT 语义违规各重试最多两次；Roller 当前让 OpenClaw 处理 HTTP
  transport retry，语义违规在 transcript 收尾时把 sample 标为 error，不额外调用模型；
- claw-eval 可在 THINK 已消费超过阈值的大 tool result 后，仅对配对 ACT 使用 placeholder；
  Roller 不做这层临时 context 改写，实际 context 仍由 OpenClaw 的 compaction/pruning 管理。

## 6. 产出文件

### `trace.jsonl`

OpenClaw 保留直接的 `toolCall` 与 `toolResult`：thoughts 位于
`message.content[].arguments.thoughts`，结果为 `------`。

Codex 保留其真实 native transport：模型响应是名为 `exec` 的 `custom_tool_call`，其
JavaScript input 调用 MCP handle；MCP 完成事件保存明确 reasoning，常见形态是
`event_msg.item_completed.item(type="McpToolCall").arguments.thoughts`，另一条 session writer
路径会记录 `event_msg.mcp_tool_call_end.invocation.arguments.thoughts`；随后
`custom_tool_call_output` 包含 `------`。这是 Codex 的原始事实，不会在 raw 中伪装成顶层
function call。

### `trace_ACTF.json`

两种 Harness 都规范化为同一结构：`deep_think` 位于
`assistant_content.tool_calls`，对应结果位于 `observation`。Codex 转换器同时接受上述两种
native MCP completion event，并使用真实 server/tool/arguments 把外层 `exec` 包装还原为
canonical `deep_think`。Roller 不在 ACTF 中把它复制到 `reasoning_content`，避免重复。

### `trace_SFT.json` / `sft.jsonl`

SFT 不要求训练后的模型继续调用内部 scratchpad 工具。转换器执行以下投影：

- 删除 `deep_think` call、对应的 `------` tool result、顶层 schema 和 system prompt
  工具清单中的对应条目；
- 将独立 THINK response 的 thoughts 写入随后 ACT assistant 的 `reasoning_content`；
- 若 think 与业务工具出现在同一个 assistant，则写入该 assistant 的
  `reasoning_content`；
- 保留所有业务 tool call/result；若已有可见 native reasoning，则按时间顺序以空行连接。

顶层仍严格只有 `messages` 与 `tools`。该 `reasoning_content` 是训练投影，不是 provider
原生 reasoning 字段的 wire 证据；检查真实协议必须查看 raw 或 full wire。

Claude Code Harness 当前不实现 external thinking。`--harness claude-code` 只接受
`--external-thinking off`，并从 Claude native session 导出服务实际返回的明文 thinking。
该限制与 Anthropic Messages API 的调用和轨迹采集无关；见
[Claude Code Harness 指南](claude-code-configuration.md)。

### `wire_trace.jsonl`

`--wire-trace full` 在支持 external thinking 的 OpenClaw 和 Codex 中都保存代理
实际转发的 body，并增加诊断字段：

```json
{"external_thinking_phase":"think","body":{"reasoning":{"effort":"none"}}}
```

`external_always` 应依次出现 `think/act/think/act`；`external` 首次为 `think`，之后为
`optional`。Codex THINK body 中只保留 programmatic `functions.exec`，ACT body 恢复原始
`additional_tools`；OpenClaw 则直接改变 function tools。request-only wire 不保存响应 body。

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

## 7. 如何查看和验收

Viewer 读取 ACTF，因此会把 external reasoning 显示为 `deep_think` 工具调用，而不是已经
折叠的 SFT reasoning：

```bash
roller view runs/examples_codex_external_always
```

在页面中应看到 THINK step 的 `deep_think` arguments、固定结果 `------`，以及随后独立的
业务工具或 final step。要核对训练记录，应直接检查 `trace_SFT.json`：

```bash
sample=runs/examples_codex_external_always/samples/<model>/<task>
jq '.runtime.external_thinking' "$sample/info.json"
jq '[.steps[].assistant_content.tool_calls[]?.name]' "$sample/trace_ACTF.json"
jq '[.messages[] | select(.role == "assistant") |
    {reasoning_content, content, tool_calls}]' "$sample/trace_SFT.json"
```

使用 `--wire-trace full` 时，还可核对实际转发阶段：

```bash
jq -c '{seq, phase: .external_thinking_phase,
        effort: .body.reasoning.effort,
        tool_choice: .body.tool_choice}' "$sample/wire_trace.jsonl"
```

成功的 `external_always` 应从 `think` 开始，严格交替到 `act`；每条请求的 effort 都应为
`none`。成功的 `external` 首条为 `think`，后续为 `optional`。

## 8. 常见错误

### `Codex external thinking requires --api-base`

External thinking 必须在最终 Responses HTTP 边界控制协议，因此不能只依赖 Codex 内置
provider。传入绝对的 `http://.../v1` 或 `https://.../v1`。

### 要求 `--network host` 或 `--no-codex-websockets`

Codex 容器需要访问宿主机上的 rollout-local HTTP proxy。保持默认 network，并关闭
Responses WebSocket；这两个条件在容器启动前校验。

### `programmatic custom exec` 缺失

Codex 没有在 Responses `additional_tools` 中暴露 Roller 已验证的 custom `exec`。使用固定
的 `roller-codex:0.149.0`，或在升级镜像后重新验证 programmatic tool calling，不做静默
降级。

### `external_always ... unpaired THINK` 或 ACT 阶段再次调用 `deep_think`

模型没有遵循严格交替协议。该 sample 会保留 raw/ACTF 供排查，但状态设为 `error`，不会
进入 run 级 `sft.jsonl`。检查 full wire 的 phase、instructions 和 API 是否完整回放 custom
tool call/output history。

### `deep_think ... expected placeholder`

MCP 调用失败、被替换，或 custom `exec` 没有把 MCP 返回内容交给 `text(...)`。Roller 只有
看到成功的 `------` result 才把该阶段视为完成，避免失败的 JavaScript 被误算成 THINK。

## 9. 安全与训练注意事项

- thoughts 是完整可见的模型输出，可能包含敏感输入、凭证、个人信息或错误推断；按训练
  数据而不是普通日志管理。
- `--no-keep-thinking` 不删除 external thoughts；它们在 raw/ACTF 中仍是工具参数，在 SFT
  中仍投影为 `reasoning_content`。要禁止保存显式 CoT，必须使用
  `--external-thinking off`。
- `external_always` 会增加 token、延迟和 API 请求数；比较模型成本时不要和 native 模式
  直接混用而不标注协议。
- SFT 已移除 `deep_think` 协议，不要求部署时提供该工具；训练框架必须支持 assistant
  `reasoning_content`，或在数据预处理阶段把它转换成目标框架的 scratchpad 表示。
