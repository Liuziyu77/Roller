# SFT data format

[Documentation home](../index.md) · [Trajectory outputs](trajectory-outputs.md) · [Task format](../tasks/task-format.md)

While keeping the raw trace and ACTF, Roller also generates one training-only
`trace_SFT.json` for every successful trajectory. Run-root `sft.jsonl` aggregates
every successful sample in request-index order. Each line is identical to the
corresponding `trace_SFT.json`.

## 1. Top-level structure

An SFT record has strictly only `messages` and `tools` at the top level:

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
| Top-level `tools` | Top-level `tools` |

`metric`, `started_at`, `finished_at`, and observation `toolName`, `isError`,
and `details` do not enter SFT. The text of a tool error is still kept as-is.
GPT Responses encrypted reasoning does not enter the training data. Only the
visible reasoning summary in ACTF enters `reasoning_content`.

## 4. Data integrity

The exporter requires:

- Every assistant tool call has a non-empty, globally unique ID;
- Every call has exactly one matching tool result in the same step;
- The call's tool name exists in the top-level tools schema;
- Arguments can be encoded as a JSON object;
- Tool results contain only strings or text blocks.

When Roller encounters a non-text tool result such as an image, it does not
silently delete the content. The sample is marked as a trajectory conversion
error. Multimodal SFT needs a separately defined content-block schema.

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
