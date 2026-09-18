# SFT 数据格式

[文档中心](../index.md) · [轨迹产物](trajectory-outputs.md) · [Task 格式](../tasks/task-format.md) · [External Thinking](external-thinking.md)

Roller 在保留 raw trace 和 ACTF 的同时，为每条成功轨迹生成一份只用于训练的
`trace_SFT.json`。Run 根目录的 `sft.jsonl` 按 request index 汇总所有成功 sample，
每行与对应的 `trace_SFT.json` 内容相同。

## 1. 顶层结构

纯文本 SFT record 顶层严格只有 `messages` 和 `tools`：

```json
{
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "读取 input.txt"},
    {
      "role": "assistant",
      "reasoning_content": "需要先读取文件。",
      "content": "",
      "tool_calls": [
        {
          "id": "call-1",
          "type": "function",
          "function": {
            "name": "read",
            "arguments": "{\"path\":\"input.txt\"}"
          }
        }
      ]
    },
    {"role": "tool", "tool_call_id": "call-1", "content": "file contents"},
    {
      "role": "assistant",
      "reasoning_content": "根据结果回答。",
      "content": "文件内容是……",
      "tool_calls": []
    }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "read",
        "description": "Read a file",
        "parameters": {
          "type": "object",
          "properties": {"path": {"type": "string"}},
          "required": ["path"]
        }
      }
    }
  ]
}
```

SFT record 不包含 schema version、task/model、usage、时间戳、step ID、stop reason、
运行路径或其他 metadata。这些信息仍保留在相邻的 `info.json` 和 `trace_ACTF.json`。

### 多模态 record

当工具结果携带图片、且 ACTF 层已无损捕获为 base64 字节时，record 会额外增加顶层
`images` 列表。图片不再内联保留：它在消息文本中的位置变成保留占位符 `<image>`，
归一化后的 data URI 追加进 `images`：

```json
{
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "描述 chart.png"},
    {
      "role": "assistant",
      "reasoning_content": "",
      "content": "",
      "tool_calls": [
        {
          "id": "call-1",
          "type": "function",
          "function": {
            "name": "read",
            "arguments": "{\"path\":\"chart.png\"}"
          }
        }
      ]
    },
    {"role": "tool", "tool_call_id": "call-1", "content": "Image size: 2x1\n<image>"},
    {"role": "assistant", "reasoning_content": "", "content": "图表内容是……", "tool_calls": []}
  ],
  "tools": [],
  "images": ["data:image/png;base64,iVBORw0KGgo…"]
}
```

约定如下：

- 从前到后扫描 `messages`，第 N 个 `<image>` 占位符对应 `images[N-1]`，因此占位符
  数量恒等于 `len(images)`。
- `images` 元素是完整的 `data:image/png|jpeg|webp|gif;base64,…` URI，不保存裸
  base64。相同图片不去重：列表编码的是出现顺序，不是 blob 存储。
- 纯文本 record 永远不会出现 `images` 键，现有纯文本数据和消费方完全向后兼容。
- `<image>` 是保留占位符；含图片的 record 若源文本中已出现无法与占位符区分的字面
  `<image>`，转换直接失败。

## 2. Message 规则

- 第一条固定为 `system`；没有捕获到 system prompt 时 `content` 为 `""`。
- `user` 只包含 `role` 和字符串 `content`。
- `assistant` 始终包含 `role`、`reasoning_content`、`content` 和 `tool_calls`；
  没有 reasoning、可见文本或工具调用时分别使用 `""`、`""`、`[]`。
- `tool_calls[*]` 使用 function-call 结构，`function.arguments` 是紧凑 JSON 字符串。
- `tool` 只包含 `role`、`tool_call_id` 和字符串 `content`。
- 工具调用后的 assistant 直接跟在对应 tool message 后，不插入空 user message。
- `tools` 保留模型实际看到的 function schema；没有可用 schema 且轨迹未调用工具时为 `[]`。

## 3. ACTF 映射

| ACTF | SFT |
| --- | --- |
| `system_prompt` | 第一条 system message |
| `steps[*].user_content` | user message（非空时） |
| `steps[*].assistant_content` | assistant message |
| `steps[*].observation` | tool message |
| `steps[*].observation[*].content[*]` 的 image block | tool message 中的 `<image>` 占位符，外加一条顶层 `images` 记录 |
| 顶层 `tools` | 顶层 `tools` |

`metric`、`started_at`、`finished_at`、observation 的 `toolName`、`isError` 和
`details` 不进入 SFT。工具错误的文本内容仍原样保留。GPT Responses 的 encrypted
reasoning 不进入训练数据；只有 ACTF 中可见的 reasoning summary 会进入
`reasoning_content`。

启用 `--external-thinking` 时，raw/ACTF 保持真实 `deep_think` 调用协议，专用 SFT 则把它
标准化成随后实际 action 对应 assistant 的 `reasoning_content`：

```json
{
  "role": "assistant",
  "reasoning_content": "完整、显式的中间推理",
  "content": "",
  "tool_calls": [{
    "id": "call_business",
    "type": "function",
    "function": {
      "name": "read",
      "arguments": "{\"path\":\"input.txt\"}"
    }
  }]
}
```

转换器删除 `deep_think` tool call、其 `------` result、顶层 schema 和 system prompt
工具清单中的对应条目；独立 THINK message
的 thoughts 合并进下一条 ACT assistant，同一 response 内与业务工具并列的 think 则合并进
当前 assistant。业务 tool call/result 原样保留。若原有 `reasoning_content` 也非空，各段按
顺序以空行连接；孤立且没有后续 action 的 think 会令 SFT 转换失败，不会静默丢失。

Codex native raw 使用外层 programmatic `exec` 调用本地 MCP；ACTF 转换器先依据真实
MCP completion event（`item_completed/McpToolCall` 或 `mcp_tool_call_end`）把它还原为
canonical `deep_think`，之后与 OpenClaw 共用同一套 SFT 折叠逻辑。因此专用 SFT 不包含
`exec` 包装、MCP server 名或 `------` 占位结果。

这里的 `reasoning_content` 是 Roller 面向训练的标准化字段，不声称 provider 原生返回了
该字段。`--no-keep-thinking` 不删除显式 external CoT；要排除它必须使用
`--external-thinking off`。详细模式说明见 [External Thinking](external-thinking.md)。

## 4. 数据完整性

导出器要求：

- 每个 assistant tool call 都有非空且全局唯一的 ID；
- 每个 call 在同一 step 内恰好有一个匹配的 tool result；
- call 的工具名存在于顶层 tools schema；
- arguments 可以编码为 JSON object；
- 工具结果只包含字符串、text blocks 或内嵌 base64 字节的 image blocks。

image block 接受三种形态：base64 `source` 结构（Claude Code）、扁平 `data` +
`mimeType` 结构（OpenClaw/Pi 原生）和内联 data URI。media type 必须是
`image/png`、`image/jpeg`、`image/webp` 或 `image/gif`，payload 必须能通过严格
base64 解码。其余情况——audio 等未知 block 类型、不支持的 media type、损坏的
base64、只引用文件路径而没有字节的 image block——仍然令转换失败并把 sample 标记为
trajectory conversion error；Roller 不会静默删除内容。转换器本身不读文件也不访问
网络：若某 harness 的读图工具只留下路径，必须在文件仍可访问时由对应 ACTF 层把
字节物化成 image block。

## 5. 文件位置

```text
<run>/
├── lock.json
├── sft.jsonl
└── samples/<model>/<index>_<task>/
    ├── trace.jsonl
    ├── trace_ACTF.json
    └── trace_SFT.json
```

User Simulator rollout 的任务 Agent SFT 位于根 sample，模拟用户自己的 SFT 位于
`user_simulator/trace_SFT.json`；run 级 `sft.jsonl` 只汇总任务 Agent。
