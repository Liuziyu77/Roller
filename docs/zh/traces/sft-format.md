# SFT 数据格式

Roller 为每条成功轨迹生成训练文件 `trace_SFT.json`，同时保留 raw trace 和 ACTF。Run 根目录的 `sft.jsonl` 按 request index 汇总所有成功 sample，
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

当 user message 或工具结果携带图片、且 ACTF 层已无损捕获为 base64 字节时，record
会额外增加顶层 `images` 列表。图片不再内联保留：它在消息文本中的位置变成保留占位符 `<image>`，
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
- `<image>` 是保留占位符；检查覆盖消息正文、reasoning 和工具调用参数。含图片的 record 若源文本中已出现无法与占位符区分的字面
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
| `steps[*].user_content` | user message（非空时）；image block 变为 `<image>` 并追加顶层 `images` |
| `steps[*].assistant_content` | assistant message |
| `steps[*].observation` | tool message |
| `steps[*].observation[*].content[*]` 的 image block | tool message 中的 `<image>` 占位符，外加一条顶层 `images` 记录 |
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
- user content 和工具结果只包含字符串、text blocks 或内嵌 base64 字节的 image blocks。

image block 接受三种形态：base64 `source` 结构（Claude Code）、扁平 `data` +
`mimeType` 结构（OpenClaw/Pi 原生）和内联 data URI。media type 必须是
`image/png`、`image/jpeg`、`image/webp` 或 `image/gif`，payload 必须能通过严格
base64 解码，随后重新编码为规范形式（消除可解码的冗余 padding，图片字节不变）。
导出、汇总与 Resume 使用同一套解析规则；保存的 `images` 必须采用规范形式。
严格解码沿用当前 Python 标准库的行为：冗余 padding 在部分版本中会直接被拒绝，
不保证跨版本接受。被拒绝的样本保留 raw/ACTF、标记转换失败并排除出 SFT 汇总，
不影响其他正常样本完成汇总；Roller 不会为兼容异常 padding 放宽解码规则。
其余情况——audio 等未知 block 类型、不支持的 media type、损坏的
base64、只引用文件路径而没有字节的 image block——仍然令转换失败。
Collector 会在 `runtime.trajectory_conversion` 中记录 `target: "sft"`、
`status: "unavailable"` 和原因，保留 raw/ACTF，不生成 SFT、不计入汇总。
这不会把已完成的采集改成失败；Resume 复用已有采集，不会为此重新调用模型。
Roller 不会静默删除内容。转换器本身不读文件也不访问
网络：若某 harness 的读图工具只留下路径，必须在文件仍可访问时由对应 ACTF 层把
字节物化成 image block。

上述是 SFT 层接受的内容块形态，不代表所有 Harness 的运行时都能调用视觉能力。
各 Harness 的 image 工具结果处理如下：

| Harness | 运行时图片路径 | ACTF/SFT 图片保存 |
| --- | --- | --- |
| OpenClaw | Prompt 附件、`read` 或 `image` 辅助模型调用 | 保留；紧凑 `actf` wire 模式只暂存所需图片输入 |
| Codex | 通过 Responses 调用 `view_image` | 保留 |
| Claude Code | `Read` 图片结果 | 保留 |
| OpenCode | `read` 返回内嵌 data URL 附件 | 保留；非图片附件只保留有界审计元数据 |
| Hermes | Roller 为固定 profile 启用 `vision_analyze` 工具集 | 保留 |
| DeepSeek | 面向文本的 Bash profile | 显式拒绝 image 内容 |
| Pi | `read` 原生图片结果，并把所选模型注册为支持图片 | 保留 |

OpenCode 图片附件若只有远程 URL 或文件路径，仍会保留在 ACTF，但无法形成多模态
SFT，因为导出过程不会读取文件或网络。该样本会保留 raw/ACTF，并把 SFT 记录为不可用。

## 5. 文件位置

```text
<run>/
├── lock.json
├── sft.jsonl
└── samples/<model>/<index>_<task>/
    ├── trace.jsonl
    ├── trace_ACTF.json
    ├── trace_SFT.json
    └── subagents/
        └── <index>_<session>/
            ├── trace.jsonl
            ├── trace_ACTF.json
            └── trace_SFT.json
```

OpenClaw 子代理 session 会独立导出，父子关系和路径记录在
`info.json.runtime.subagent_trajectories`。它们不会被扁平拼接进主会话，也不会追加到
run 级 `sft.jsonl`，从而在保留子代理图片输入的同时维持每条模型线程的语义。未完成的
子代理也可以保留部分 raw/ACTF/SFT sidecar。

User Simulator rollout 的任务 Agent SFT 位于根 sample，模拟用户自己的 SFT 位于
`user_simulator/trace_SFT.json`；run 级 `sft.jsonl` 只汇总任务 Agent。
