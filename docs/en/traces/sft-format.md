# SFT data format

Roller generates a training file, `trace_SFT.json`, for each successful trajectory,
alongside the raw trace and ACTF. Run-root `sft.jsonl` aggregates
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
- Collision checks cover message content, reasoning, and tool-call arguments.
  Because `<image>` is reserved, a record with images fails conversion when
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
- `tools` keeps captured tool definitions. Codex preserves the native Responses list (flat functions, namespaces, custom grammars, and built-in tools), including unused tools and all original schema fields; consumers must support this format. Other harnesses keep the existing nested function representation. Codex requires captured definitions even when no tools were called; missing capture never becomes an empty list.
- Codex custom calls use `{"id":"...","type":"custom","custom":{"name":"...","input":"original string"}}`. Function/custom call namespace fields are preserved. Changing schemas or programmatic `additional_tools` items retain their exact snapshots in ACTF and make SFT explicitly unavailable.

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

## 4. Data integrity

The exporter requires:

- Every assistant tool call has a non-empty, globally unique ID;
- Every call has exactly one matching tool result in the same step;
- The call's tool name exists in the top-level tools schema;
- Function arguments can be encoded as a JSON object; Codex custom input remains a string;
- Tool results contain only strings, text blocks, or image blocks with
  embedded base64 bytes.

Image blocks are accepted in the base64 `source` form (Claude Code), the flat
`data` + `mimeType` form (OpenClaw/Pi native), and the inline data-URI form.
The media type must be `image/png`, `image/jpeg`, `image/webp`, or
`image/gif`, and the payload must decode as strict base64. It is then re-encoded
canonically, removing decodable redundant padding without changing the image
bytes. Export, aggregation, and Resume share the same parser; saved `images`
must use the canonical representation. Strict decoding follows the current Python
standard library: some releases reject excess padding, so its acceptance is not
guaranteed across versions. Rejected samples retain raw/ACTF, are marked as
conversion failures, and are excluded from SFT aggregation without preventing
valid samples from being aggregated. Roller does not relax decoding to accept
invalid padding. Anything else —
audio or other unknown block types, unsupported media types, malformed
base64, or an image block that only references a file path without bytes —
still fails conversion. The Collector records `target: "sft"`,
`status: "unavailable"`, and the reason in `runtime.trajectory_conversion`,
retains raw/ACTF, and omits SFT and its aggregate entry. This does not turn a
completed collection into a failure; Resume reuses the collected sample without
calling the model again. Roller never silently drops content. The converter itself never reads files
or networks: a harness whose image tool only leaves a path must materialize
the bytes into the ACTF layer while the file is still accessible.

These are accepted SFT block shapes, not a claim that every Harness can invoke
vision at runtime. Image tool results are handled as follows:

| Harness | Runtime image path | ACTF/SFT image preservation |
| --- | --- | --- |
| OpenClaw | Image-capable model and image tool | Preserved |
| Codex | `view_image` through Responses | Preserved |
| Claude Code | `Read` image result | Preserved |
| OpenCode | `read` attachment with an inline data URL | Preserved; non-image attachments retain bounded audit metadata |
| Hermes | Roller enables the pinned profile's `vision_analyze` toolset | Preserved |
| DeepSeek | Text-oriented Bash profile | Image content is rejected explicitly |
| Pi | Native `read` image result with image-capable model registration | Preserved |

An OpenCode image attachment that only carries a remote URL or path remains in
ACTF but cannot form multimodal SFT, because export never reads files or
networks. The sample therefore retains raw/ACTF and records SFT as unavailable.

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
