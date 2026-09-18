# SFT data format

[Documentation home](../index.md) · [Trajectory outputs](trajectory-outputs.md) · [Task format](../tasks/task-format.md) · [External Thinking](external-thinking.md)

While keeping the raw trace and ACTF, Roller also generates one training-only
`trace_SFT.json` for every successful trajectory. Run-root `sft.jsonl` aggregates
every successful sample in request-index order. Each line is identical to the
corresponding `trace_SFT.json`.

## 1. Top-level structure

A text-only SFT record has exactly `messages` and `tools` at the top level:

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

An SFT record does not include schema version, task/model, usage, timestamps,
step ID, stop reason, run path, or other metadata. That information remains in
the adjacent `info.json` and `trace_ACTF.json`.

### Multimodal records

When a tool result carries an image that the ACTF layer captured losslessly as
base64 bytes, the record additionally has a top-level `images` list. The image
does not stay inline: its position in the message text becomes the reserved
placeholder `<image>`, and the normalized data URI is appended to `images`:

```json
{
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Describe chart.png"},
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
    {"role": "assistant", "reasoning_content": "", "content": "The chart…", "tool_calls": []}
  ],
  "tools": [],
  "images": ["data:image/png;base64,iVBORw0KGgo…"]
}
```

The contract is:

- Scanning `messages` front to back, the Nth `<image>` placeholder corresponds
  to `images[N-1]`, so the placeholder count always equals `len(images)`.
- `images` entries are complete `data:image/png|jpeg|webp|gif;base64,…` URIs;
  raw base64 is never stored. Identical images are not deduplicated: the list
  encodes the occurrence sequence, not a blob store.
- Text-only records never gain an `images` key, so existing pure-text data and
  consumers stay backward compatible.
- Because `<image>` is reserved, a record with images fails conversion when
  source text already contains a literal `<image>` that cannot be told apart
  from a placeholder.

## 2. Message rules

- The first message is always `system`. When no system prompt was captured,
  `content` is `""`.
- `user` contains only `role` and string `content`.
- `assistant` always contains `role`, `reasoning_content`, `content`, and
  `tool_calls`. When there is no reasoning, visible text, or tool call, the
  values are `""`, `""`, and `[]` respectively.
- `tool_calls[*]` uses the function-call structure. `function.arguments` is a
  compact JSON string.
- `tool` contains only `role`, `tool_call_id`, and string `content`.
- The assistant after a tool call follows the matching tool message directly.
  No empty user message is inserted.
- `tools` keeps the function schema the model actually saw. It is `[]` when no
  schema is available and the trajectory did not call tools.

## 3. ACTF mapping

| ACTF | SFT |
| --- | --- |
| `system_prompt` | First system message |
| `steps[*].user_content` | user message (when non-empty) |
| `steps[*].assistant_content` | assistant message |
| `steps[*].observation` | tool message |
| `steps[*].observation[*].content[*]` image block | `<image>` placeholder in the tool message plus one top-level `images` entry |
| Top-level `tools` | Top-level `tools` |

`metric`, `started_at`, `finished_at`, and observation `toolName`, `isError`,
and `details` do not enter SFT. The text of a tool error is still kept as-is.
GPT Responses encrypted reasoning does not enter the training data. Only the
visible reasoning summary in ACTF enters `reasoning_content`.

When `--external-thinking` is enabled, raw/ACTF keep the real `deep_think` call
protocol. Dedicated SFT normalizes it into the `reasoning_content` of the
assistant that corresponds to the following actual action:

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

The converter deletes the `deep_think` tool call, its `------` result, the
top-level schema, and the matching entry in the system-prompt tool list.
Thoughts from a standalone THINK message are merged into the next ACT
assistant. A think that sits beside a business tool in the same response is
merged into the current assistant. Business tool calls/results are kept as-is.
If existing `reasoning_content` is also non-empty, the segments are joined in
order with a blank line. An isolated think with no following action fails SFT
conversion; the content is not silently dropped.

Codex native raw uses an outer programmatic `exec` to call local MCP. The ACTF
converter first restores it to canonical `deep_think` from the real MCP
completion event (`item_completed/McpToolCall` or `mcp_tool_call_end`), then
shares the same SFT folding logic with OpenClaw. Dedicated SFT therefore
contains no `exec` wrapper, MCP server name, or `------` placeholder result.

The `reasoning_content` here is Roller's training-oriented normalized field. It
does not claim the provider natively returned that field.
`--no-keep-thinking` does not delete explicit external CoT. To exclude it you
must use `--external-thinking off`. See [External Thinking](external-thinking.md)
for the full mode description.

## 4. Data integrity

The exporter requires:

- Every assistant tool call has a non-empty, globally unique ID;
- Every call has exactly one matching tool result in the same step;
- The call's tool name exists in the top-level tools schema;
- Arguments can be encoded as a JSON object;
- Tool results contain only strings, text blocks, or image blocks with
  embedded base64 bytes.

Image blocks are accepted in the base64 `source` form (Claude Code), the flat
`data` + `mimeType` form (OpenClaw/Pi native), and the inline data-URI form.
The media type must be `image/png`, `image/jpeg`, `image/webp`, or
`image/gif`, and the payload must decode as strict base64. Anything else —
audio or other unknown block types, unsupported media types, malformed
base64, or an image block that only references a file path without bytes —
still fails conversion and marks the sample as a trajectory conversion error;
Roller never silently drops content. The converter itself never reads files
or networks: a harness whose image tool only leaves a path must materialize
the bytes into the ACTF layer while the file is still accessible.

## 5. File locations

```text
<run>/
├── lock.json
├── sft.jsonl
└── samples/<model>/<index>_<task>/
    ├── trace.jsonl
    ├── trace_ACTF.json
    └── trace_SFT.json
```

For a User Simulator rollout, the Task Agent SFT is in the root sample. The
simulated user's own SFT is at `user_simulator/trace_SFT.json`. Run-level
`sft.jsonl` aggregates only the Task Agent.
