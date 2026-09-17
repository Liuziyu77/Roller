# SFT 数据格式

[文档中心](../index.md) · [轨迹产物](trajectory-outputs.md) · [Task 格式](../tasks/task-format.md)

Roller 为每条成功轨迹生成训练文件 `trace_SFT.json`，同时保留 raw trace 和 ACTF。Run 根目录的 `sft.jsonl` 按 request index 汇总所有成功 sample，
每行与对应的 `trace_SFT.json` 内容相同。

## 1. 顶层结构

SFT record 的顶层只包含 `messages` 和 `tools`：

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
| 顶层 `tools` | 顶层 `tools` |

`metric`、`started_at`、`finished_at`、observation 的 `toolName`、`isError` 和
`details` 不进入 SFT。工具错误的文本内容仍原样保留。GPT Responses 的 encrypted
reasoning 不进入训练数据；只有 ACTF 中可见的 reasoning summary 会进入
`reasoning_content`。

## 4. 数据完整性

导出器要求：

- 每个 assistant tool call 都有非空且全局唯一的 ID；
- 每个 call 在同一 step 内恰好有一个匹配的 tool result；
- call 的工具名存在于顶层 tools schema；
- arguments 可以编码为 JSON object；
- 工具结果只包含字符串或 text blocks。

遇到 image 等非文本工具结果时，Roller 会将该 sample 标记为 trajectory conversion error，
避免在导出时丢失内容。多模态 SFT 需要单独定义 content-block schema。

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
