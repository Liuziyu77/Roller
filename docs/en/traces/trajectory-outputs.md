# Trajectory output file guide

[Documentation home](../index.md) · [Getting started](../start/getting-started.md) · [SFT format](sft-format.md) · [Viewer](viewer.md)

This page explains which files `roller collect` produces, when each file is created
and retained, how their information differs, and which layer to use for training,
analysis, and debugging.

## 1. Start with the conclusion

| Need | Preferred file | Why |
| --- | --- | --- |
| Train a Task Agent directly | Run-level `sft.jsonl` | One minimal SFT record per line, ready for streaming reads |
| Inspect a single training sample | Sample-level `trace_SFT.json` | Identical to the matching record in `sft.jsonl` |
| Trajectory analysis, scoring, token stats | `trace_ACTF.json` | Has step, reasoning, observation, metric, and timing |
| Trace Harness session raw events | `trace.jsonl` | OpenClaw, Codex, Claude Code, or Hermes native session; Roller does not clean it for training |
| Inspect the first system prompt captured into ACTF | Top level of `trace_ACTF.json` | Taken from whichever native/cache/wire source each Harness can obtain, frozen once |
| Check whether the OpenClaw system prompt changes per call | `cache_trace.jsonl` from `--wire-trace full` | Keeps a diagnostic record of every OpenClaw LLM call |
| Check the request history and tools the model actually received | `wire_trace.jsonl` from `--wire-trace full` | Saves each JSON request body the proxy actually forwarded |
| Audit Claude signature recovery | `signature_recovery.jsonl` | Saves attempt, quality, usage, and recovered text for each signed block; does not copy the signature |
| Inspect Task Agent delivered files | `results/` | Recovers only the output directories the task declared plus extra collect paths |
| Inspect the dialogue both sides can see | `conversation.json` | Does not include hidden reasoning, tool calls, or tool results |

Use SFT for training, ACTF for analysis, and only drop down to session raw and
full wire when debugging an adapter, context assembly, or upstream behavior. In
this page, **session raw** means `trace.jsonl` only; `wire_trace.jsonl` is a
request-side diagnostic record. Do not treat the two as the same kind of raw.
Do not treat a wire trace as a training set either: multi-turn requests repeat
history, so the size is close to O(N²).

## 2. Directory layout

### 2.1 Default ordinary rollout

OpenClaw defaults are `--cache-trace --wire-trace actf --keep-thinking`; Codex
disables web search by default and keeps the reasoning content or summary that
is actually visible in the native session; Claude Code keeps plaintext thinking
blocks from the native session. The main layout of a successful run is the same
for all four Harnesses:

```text
<output>/<run-name>/
├── info.json
├── lock.json
├── sft.jsonl
└── samples/
    └── <model>/
        └── <index>_<task>/
            ├── trace.jsonl
            ├── trace_ACTF.json
            ├── trace_SFT.json
            ├── signature_recovery.jsonl  # Claude Code 显式开启 recovery 时存在
            ├── info.json
            └── results/                 # Task 输出含文件或目录时存在
```

OpenClaw's default `actf` wire mode temporarily captures the tools schema, writes
it into ACTF/SFT, then deletes the intermediate `wire_trace.jsonl` and
`cache_trace.jsonl`, so the final sample stays compact. Codex's default `actf`
builds a permissive tool schema from the native session and does not start a
request proxy. Claude Code temporarily captures the Anthropic system and full
`input_schema` when `--api-base` is set explicitly; with no API base or with
`--wire-trace off`, it derives a permissive schema from actual `tool_use`.

### 2.2 `--wire-trace full`

`full` mode also keeps the diagnostic layer:

```text
<index>_<task>/
├── trace.jsonl
├── trace_ACTF.json
├── trace_SFT.json
├── signature_recovery.jsonl       # Claude Code 显式开启 recovery 时存在
├── cache_trace.jsonl              # 仅 OpenClaw
├── wire_trace.jsonl
├── info.json
└── results/                       # 可选
```

### 2.3 User Simulator rollout

A dynamic task stores the Task Agent in the root sample and the Simulator in a
subdirectory:

```text
<index>_<task>/
├── trace.jsonl                    # Task Agent raw
├── trace_ACTF.json                # Task Agent ACTF
├── trace_SFT.json                 # Task Agent SFT
├── conversation.json              # 双方可见的干净交替对话
├── info.json                      # 总 usage 与双方索引
├── user_simulator/
│   ├── trace.jsonl                # Simulator raw
│   ├── trace_ACTF.json            # Simulator ACTF
│   ├── trace_SFT.json             # Simulator SFT
│   └── info.json                  # Simulator 独立状态和 usage
└── results/                       # 只属于 Task Agent，可选
```

Run-level `sft.jsonl` aggregates only the Task Agent in the root directory. It
does not mix in User Simulator SFT.

## 3. Data path from a run to a training set

A trajectory is produced in this order:

```text
Harness session store ────────────────> trace.jsonl
  ├─ OpenClaw transcript SDK
  ├─ Codex $CODEX_HOME/sessions native rollout
  └─ Claude Code $CLAUDE_CONFIG_DIR/projects native session + subagents
                              │
                              ├─ Harness-native ACTF converter
                              └─ Harness request diagnostics
                                  ├─ OpenClaw cache + wire
                                  ├─ Codex wire
                                  └─ Claude Code Anthropic wire
                                                │
                                                v
trace.jsonl + Harness 可取得的 system/tools ──> trace_ACTF.json
Claude signed block ──> 独立 provider replay ──> signature_recovery.jsonl
                                              └─> ACTF reasoning（fill/prefer 成功时）
trace_ACTF.json ────────────────────────────────> trace_SFT.json
全部成功/Resume sample 的 trace_SFT.json ───────> run/sft.jsonl
```

`trace.jsonl` is the source of truth for semantic events in the current Harness
session. ACTF is the analysis projection of that session; SFT is the training
projection after fields that are irrelevant to training are removed. SFT does
not feed back into ACTF or raw generation.

### 3.1 The three files answer three different questions

| File | Capture point | Can answer | Cannot be used to claim |
| --- | --- | --- | --- |
| `trace.jsonl` | Harness session export layer | Which user/assistant/tool events the Agent recorded, in what order, with what usage | Which full history a given request carried; verbatim upstream HTTP request/response |
| `cache_trace.jsonl` | OpenClaw context-assembly diagnostic point | The assembled system prompt and related diagnostic stages for each LLM call | Full messages/input and final provider request parameters |
| `wire_trace.jsonl` (`full`) | After the Harness provider adapter, before the real model API | The JSON body Roller's proxy actually forwarded, including messages/input, tools, and sampling parameters | How the model service tokenizes, truncates, or routes the request internally |

### 3.2 Session history is not the model request context

A message that "exists" in `trace.jsonl` only means it entered the Harness session
record. It does not mean it was sent verbatim on every later model call. OpenClaw
builds a runtime context from the session, bounded by the context window. Its
official docs distinguish the persisted transcript from the model context:
compaction replaces older history with a summary and keeps recent messages;
pruning can drop old tool results from the in-memory prompt without rewriting
the on-disk transcript. A plugin context engine and runtime hooks can also take
part in context assembly. See
[OpenClaw Context](https://docs.openclaw.ai/concepts/context) and
[Session Management and Compaction](https://docs.openclaw.ai/reference/session-management-compaction).
The exact compaction, pruning, and persistence behavior depends on the OpenClaw
version in the image, the model profile, and the chosen context engine. Roller
does not guess those transforms from session files; it captures full wire at the
final API request boundary. Codex likewise inserts base/developer instructions,
environment context, a tool namespace, resume history, and compaction results
between the native session and the Responses input; that final shape can only be
confirmed from full wire. Claude Code's native session is also not an Anthropic
Messages request: the CLI adds system blocks, a tools schema, project/context
instructions, cache markers, and conversation-management results. When you need
empirical evidence of the model request, use Claude Code `--wire-trace full` as
well.

Differences therefore exist in both directions:

- Present in the session, absent from the model request: old tool results were
  pruned, history was compacted into a summary, or messages were filtered by
  window or provider rules;
- No matching per-event record in the session, but present in the model request:
  the assembled system prompt, tools schema, workspace/bootstrap injection, or
  per-turn runtime context / hook additions;
- Still present, but represented differently: the Harness/provider adapter can
  turn internal messages/blocks into the final Chat Completions `messages` or
  Responses `input` protocol shape.

So `trace.jsonl` is the right place to replay the Agent's logical trajectory;
`wire_trace full` is the evidence for "what JSON context that HTTP model call
saw." ACTF/SFT are both projections of session raw. They cannot reconstruct
per-request context after compaction, pruning, or ephemeral injection.

## 4. How the core trace files differ

### 4.1 `trace.jsonl`: Harness-native raw trace

OpenClaw content includes raw message events from the transcript: user,
assistant, thinking, tool call, tool result, usage, and so on. In the default
image, Roller reads events by exact session identity through the public
`openclaw/plugin-sdk/session-transcript-runtime`, then serializes them one by
one as JSONL. It does not delete, merge, or reorder valid events for training.
"Raw" here is relative to Roller's ACTF/SFT conversion. It is not a byte copy of
the underlying SQLite/JSONL file, and it is not a network-level raw request or
response. The SDK returns only transcript events that OpenClaw recognizes, so
corrupt lines or non-event records in the underlying store do not appear in the
export. Roller copies the session JSONL directly only when a legacy OpenClaw
build does not provide that SDK.

Codex content comes from the native JSONL under `$CODEX_HOME/sessions` in the
rollout container, including `session_meta`, `turn_context`, `response_item`,
`event_msg`, compaction events, and token usage. Codex raw is likewise a
Harness-private schema. It is not the `codex exec --json` terminal display
stream, and it is not an HTTP wire trace.

Claude Code content comes from this rollout's main session under
`$CLAUDE_CONFIG_DIR/projects` plus its `subagents/*.jsonl`. Roller merges by
timestamp, deduplicates by native `uuid`, and keeps user, assistant, thinking,
signature, `tool_use`, `tool_result`, usage, and sidechain fields. It is not the
`claude --output-format=stream-json` terminal stream, and it is not a
byte-for-byte concatenation of the source files.

Sample `info.json.trajectory.raw_schema` is marked `openclaw-session-jsonl`,
`codex-session-jsonl`, or `claude-code-session-jsonl` respectively.

Generation conditions:

- The Harness container has started and the finish phase can export a non-empty
  native session. The OpenClaw legacy path requires the specified session JSONL
  or the latest fallback; the Codex path requires the latest native rollout
  JSONL; the Claude Code path requires the latest main session in this
  container;
- If the Harness returns success but a non-empty raw trace cannot be obtained,
  the sample becomes `error`;
- If the Harness itself is `error` or `timeout`, raw is still kept whenever a
  transcript can still be exported.

Uses and limits:

- Suitable for tracing upstream fields, event order, and conversion issues;
- The structure belongs to the Harness and is not a stable training schema;
- It cannot by itself verify context compaction, tool-result pruning, or the
  real request after provider adaptation. All four Harnesses should use
  `--wire-trace full`;
- Chat Completions `reasoning` / `reasoning_text` may be normalized to
  `reasoning_content` before they enter OpenClaw, so later turns can be replayed.
  The thinking text is unchanged, but the field signature and the upstream HTTP
  response are no longer a verbatim match;
- `--no-keep-thinking` does not modify raw, so raw can still contain thinking
  produced in the current turn. That flag affects ACTF/SFT; it is not a raw
  redaction switch. OpenClaw can also strip later request history at the wire
  layer; Codex and Claude Code only control the export and do not rewrite
  `resume` / `--continue` context.
- Full thoughts from external thinking are explicit model output and are not
  stripped by `--no-keep-thinking`. OpenClaw raw stores `deep_think` tool call
  arguments directly; Codex raw stores the outer `exec` custom call and stores
  the real thoughts in the arguments of `item_completed/McpToolCall` or
  `mcp_tool_call_end`. ACTF unifies both as a canonical `deep_think` call; SFT
  then folds them into the following action assistant's `reasoning_content`.
  `info.json.runtime.external_thinking` stores the mode, protocol validation
  status, think count, and THINK/ACT pair count.

### 4.2 `trace_ACTF.json`: unified analysis trace

The ACTF top level stores `schema_version`, `system_prompt`, the full `tools`,
`steps`, and trajectory timing. Each assistant message becomes one step. A step
contains:

- `user_content`: user input for this turn;
- `assistant_content.reasoning_content`: visible reasoning/thinking;
- `assistant_content.content`: visible reply;
- `assistant_content.tool_calls`: tool calls;
- `observation`: tool results after those calls;
- `metric`, start/end time, and stop reason.

Generation conditions:

- The Harness has obtained `trace.jsonl`, and the matching adapter can produce
  canonical ACTF;
- OpenClaw system/tools come from cache/wire capture. When missing, system is
  `""` and tools is `null`;
- Codex system comes from native base instructions, developer/system messages,
  and `<environment_context>` before the first turn. The task user message is
  matched to the corresponding turn by content. The native session does not
  contain a full tool schema, so a permissive schema is generated from actual
  call names and argument fields. An outer programmatic `exec` for external MCP
  is restored to canonical `deep_think` from the native MCP completion event, so
  the training layer does not see the Harness transport wrapper.
- Claude Code merges streamed native events that share the same `message.id`,
  writes plaintext `thinking/reasoning/analysis` into `reasoning_content`, and
  associates `tool_use_id` with a later `tool_result`. Ciphertext
  `redacted_thinking` stays in raw and is never guessed into plaintext.
  When signature recovery is explicitly enabled, `fill` only fills native empty
  values; `prefer` replaces the native summary with recovered text that passes
  refusal and minimum-length checks. A step that adopts recovery marks the
  source in `extra.reasoning_source`.
  When wire is present, system/tools come from the Anthropic request; otherwise
  system is `""` and a permissive schema is derived from actual `tool_use`.

OpenClaw ACTF freezes only the first valid assembled system prompt from the
cache trace and the first non-empty tools schema from the wire trace at the top
level. Codex ACTF uses native system events and a schema derived from actual
calls. Claude Code ACTF uses the first captured Anthropic system/tools, and
falls back to native derivation when there is no wire. None of them store the
full messages/input of every request per step, and none of them express
system/tools changing between calls. Use full wire to inspect those changes;
OpenClaw can also combine it with the full cache trace.

As long as raw exists, Roller attempts conversion, including samples whose
Harness already failed or timed out. A failed sample may therefore still have
ACTF. A conversion failure sets the sample status to `error`.

### 4.3 `trace_SFT.json`: one minimal training sample

SFT is generated from ACTF. The top level is strictly only:

```json
{"messages": [], "tools": []}
```

It drops schema version, task/model, usage, metric, step ID, stop reason,
timestamps, run paths, and observation metadata. An assistant always stores
`role`, `reasoning_content`, `content`, and `tool_calls`. A tool message stores
only `role`, `tool_call_id`, and string `content`.

SFT expresses the logical conversation reconstructed from session events. It is
not a snapshot of each provider request. It is suitable for training, but not
for per-request replay or for proving that an earlier tool result was still
visible in a later model call.

External thinking has one more training-only projection: the `deep_think` call,
the fixed placeholder result, the tool schema, and the matching system-prompt
tool-list entry are removed. Thoughts are merged into the assistant
`reasoning_content` of the same logical action. Business tool calls and results
are unchanged. raw/ACTF still keep the actual execution structure, so do not
substitute SFT for them when auditing the protocol.

Generation conditions:

- ACTF conversion succeeded;
- There is at least one user message and one non-empty assistant message;
- Every tool call has a non-empty, globally unique ID;
- Every tool call has exactly one tool result in the same step;
- The called tool exists in the top-level tools schema;
- Arguments can be encoded as a JSON object;
- The tool result is a string or pure text blocks.

Both GPT Responses flat tools and Chat Completions nested tools are unified to
a nested function schema. When Roller encounters a non-text tool result such as
an image, it does not silently drop the content. SFT conversion fails and the
sample is marked `error`. Multimodal SFT needs a separately defined content-block
schema.

See [SFT data format](sft-format.md) for the full field definition.

### 4.4 `sft.jsonl`: run-level training set

After the Collector finishes every request, it reads sample-level
`trace_SFT.json` in request-index order and writes the run root atomically and
as a stream:

- Samples with `status=ok` are included;
- `--resume` reused samples with `status=skipped` were originally `ok`, and are
  included as well;
- `error` and `timeout` are not included, even if they kept partial
  raw/ACTF/SFT;
- An empty file is still created when there are no successful samples;
- User Simulator sub-trajectories are not included.

Each line is the same JSON object as the matching `trace_SFT.json`. No task ID
or other metadata is added. Sample provenance must be recovered from the
adjacent run/sample `info.json` and from directory location.

## 5. Diagnostic and auxiliary files

### 5.1 Run `lock.json`

`lock.json` is written before collection so one run cannot mix samples that were
parsed under different configurations. It freezes request order, the Task Bundle
digest, model/Harness configuration, the image ID, and options that change
trajectory meaning, such as thinking and wire/cache trace. Resume compares field
by field. A mismatch prints a limited set of differing paths and stops before
the Harness starts.

The lock does not store credentials: sensitive key values are replaced with
`<redacted>`; URL userinfo and fragment are removed; sensitive query values are
replaced; non-sensitive query keys such as `api-version` are kept for
comparison. Changing a credential or pass-through env value does not make the
lock mismatch. If those values would change actual model behavior, start a new
run. Configuration objects are compared directly; task/input/context reuse the
digests the Task Bundle already has.

### 5.2 Run `info.json`

The CLI writes initial run information before collection starts. The Collector
updates it on the first result, then after every additional 25 results, and also
immediately when two completion events are at least 30 seconds apart. It force
checkpoints on interrupt and during finalization. It stores:

- Roller version and collection configuration;
- Current `state`: `initializing`, `running`, `finalizing`, `finished`,
  `interrupted`, or `finalization_error`;
- Live `total/processed/pending` progress;
- Task/model request counts;
- `ok`, `error`, `timeout`, `skipped` totals;
- Token, cache, cost, and request-count totals;
- Per-sample status, error, path, and usage.

A catchable interrupt writes `state=interrupted` and tries to build a partial
`sft.jsonl` from already completed samples. `SIGKILL`, power loss, or host
failure cannot perform that last write, but atomic writes and periodic
checkpoints keep `info.json` as the previous complete snapshot. The Viewer also
scans already finalized samples.

### 5.3 Sample `info.json`

Every sample that finishes finalize writes this file, including failed samples.
It stores task/model, Harness, final status, error, attempt history, usage,
elapsed time, relative paths to raw/ACTF/SFT, output recovery information,
runtime, and input provenance. Metadata that training data deliberately drops is
kept here.

### 5.4 `cache_trace.jsonl`

OpenClaw `diagnostics.cacheTrace` records the assembled system prompt of every
LLM call. It is enabled by default. Roller copies it, recursively redacts
sensitive keys, then uses it to fill the ACTF top-level `system_prompt`.

- Default `--wire-trace actf`: used for conversion, then deleted;
- `--wire-trace full`: kept in the final sample;
- `--no-cache-trace`: not created; ACTF/SFT system content is `""`.

The ACTF top level uses only the first `stream:context.system`, falling back to
the first `session:loaded.system` when that stage is missing. If the system
prompt changes across calls, only the original cache trace kept in full mode can
show each difference. The ACTF top-level value does not represent every step.

Redaction is based on JSON keys. It does not scan secrets in the free text of
the system prompt. This file and the downstream ACTF/SFT should still be treated
as sensitive data.

(wire-trace-jsonl)=
### 5.5 `wire_trace.jsonl`

A local reverse proxy records wire data before the request is sent to the model
service:

- OpenClaw `actf` records only the model, non-message parameters, message count,
  and the first non-empty tools schema. It does not record full messages/input.
  That temporary file is deleted after conversion;
- Codex `actf` does not start a proxy and builds ACTF directly from the native
  session;
- Claude Code `actf` temporarily records the first Anthropic system/tools and
  non-message parameters when an API base is set explicitly, then deletes the
  file after conversion;
- `full` saves every complete request body and keeps it in the final sample;
- HTTP Authorization and other headers are not written to the file, but a
  complete request body may contain the system prompt, user data, tool results,
  and historical reasoning. `full` does not automatically redact that free
  content.

For a parseable JSON request, the proxy first applies the semantic rewrites the
current rollout needs, then records and forwards the same body. OpenClaw
`--no-keep-thinking` strips historical reasoning first; external thinking turns
off native reasoning and sets THINK/ACT instructions, tools, and tool choice.
In Codex full wire, `input[].type="additional_tools"` is the programmatic tool
namespace. The external THINK phase keeps only `functions.exec`; the ACT phase
restores the original namespace. Full wire therefore shows the semantic JSON
the Roller client actually handed to the model API, not the Harness draft before
rewriting.

Claude Code full wire saves the Anthropic `/v1/messages` request `system`,
`messages`, `tools`, thinking/output config, and sampling parameters. Roller
does not rewrite Claude Code message history. The transport-only `beta=true`
query and `anthropic-beta` header added by Claude Code 2.1.222 are removed
before forwarding. Full wire keeps the original target in `path`, records the
actual target in `upstream_path`, and records only the removed header names in
`dropped_request_headers`.

Limits:

- This is a request-side JSON record, not a TCP/HTTP byte-level capture.
  Serialization details such as JSON whitespace and field order are not part of
  the audit semantics;
- It does not record request headers, upstream response bodies, or streamed
  chunks. Response content should still be read from session raw/ACTF, which is
  already the event stream after Harness parsing;
- It can only prove what Roller's proxy forwarded, not whether the model service
  later truncated, tokenized, rewrote, or routed the request again;
- Wire writes are best effort: a single record failure does not block the model
  call. For a strong audit, check that `seq` is contiguous and confirm
  completeness against sample usage/request count and run logs;
- Multi-turn requests repeat history, so file size is close to O(N²). Use it
  only for audit and debugging.

### 5.6 `signature_recovery.jsonl` (Claude Code)

Created only by `--claude-signature-recovery fill|prefer`. After the original
task has fully finished, Roller reads native `thinking.signature` and uses a
separate Anthropic Messages request so the provider can unseal it and the model
can transcribe it mechanically. This is not local decryption and does not modify
the Claude Code session. Each line stores:

- Native `message_id`, block index, model, and sidechain flag;
- `status`, quality class, and native/signature/recovered lengths;
- Status, stop reason, token usage, and error for each attempt;
- `recovered_reasoning`.

The file does not store the original signature value again, and it does not
store the API key. `status=ok` still does not guarantee a verbatim restore.
`rewrite_or_unverified/unverified` must be sampled before training. `fill`
records `skipped_native` when native reasoning already exists and does not make
an extra model call. Common refusals, responses shorter than 32 characters, or
responses that only repeat the visible answer are not projected into ACTF/SFT.
Recovery tokens are counted in the sample usage `signature_recovery_*` fields.
The extra dollar cost of those requests is not included in the Claude CLI
`cost_usd`. Recovery is host-side post-processing over a direct connection. It
is not written to Claude Code `wire_trace.jsonl`; that full wire still represents
only the original Agent rollout request history.

### 5.7 How to audit context compaction or rewriting

1. Run a minimal reproducible task with `--wire-trace full`. For OpenClaw, also
   keep the default `--cache-trace`. Do not use default `actf` as a substitute
   for full: it does not save messages/input.
2. Compare each `body.messages` (Chat Completions or Anthropic Messages) or
   `body.input` (Responses) in `wire_trace.jsonl` `seq` order. Also check
   `body.system`, `body.tools`, and sampling parameters.
3. Compare the differences against session events in `trace.jsonl`. Do not
   expect a one-to-one line mapping: one Agent turn can contain multiple model
   requests, and each wire body repeats the history retained at that time.
4. For OpenClaw, inspect the system prompt of each `stream:context` in
   `cache_trace.jsonl`. For Codex, compare `instructions`, developer input, and
   environment context across full-wire requests. For Claude Code, compare
   `system/messages/tools` and thinking/output config.
5. If OpenClaw used `--no-keep-thinking`, treat full wire as the evidence of
   model-visible history. Reasoning kept in session raw is diagnostic only and
   does not mean it was sent on later requests. For Codex and Claude Code, that
   flag only controls the export.

After the audit, protect or delete cache/wire files separately. They may contain
repeated user input, system prompts, tool results, reasoning, file contents, and
runtime injections.

### 5.8 `results/`

Created only when the task output directory contains ordinary files or
directories. Roller:

- Copies every safe ordinary file and keeps the relative path;
- Records path, size, MIME, and SHA-256 in sample `info.json`;
- Rejects symbolic links and special files;
- Still copies safe files even when unsafe files are present, but the sample's
  final status becomes `error`;
- Places compatible `--collect-path` output in `results/legacy/`.

`results/` is Agent deliverable output. It is not conversation training data and
is not written into `sft.jsonl`.

### 5.9 `conversation.json`

Created only for User Simulator tasks. It stores the user/assistant turns both
sides can see, the termination reason, instructions that were pending but not
delivered, participating models, and timing. It does not copy hidden reasoning,
tool calls, or tool results from either side. To train or analyze either
participant, use that participant's own raw/ACTF/SFT.

## 6. How flag combinations change traces

### 6.1 Wire trace

| Harness / mode | Proxy requirement | ACTF tools | Final auxiliary files | When to use |
| --- | --- | --- | --- | --- |
| OpenClaw `actf` (default) | Enabled when a proxyable `baseUrl` exists; degrades when missing | Full schema when capture succeeds, otherwise `null` | cache/wire not kept | Regular batch collection |
| Codex `actf` (default) | Does not start a proxy | Permissive schema from native actual calls | wire not kept | Regular batch collection |
| Claude Code `actf` (default) | Starts when `--api-base` is set explicitly | Captures Anthropic `input_schema`; derives from native when there is no proxy | wire not kept | Regular batch collection |
| OpenClaw `full` | Provider must have an absolute `baseUrl` | Full schema | `wire_trace`, plus `cache_trace` when cache is enabled | Request-level audit |
| Codex `full` | Must set `--api-base --network host` explicitly | Native permissive schema | `wire_trace` | Request-level audit |
| Claude Code `full` | Must set `--api-base --network host` explicitly | Captures Anthropic `input_schema` | `wire_trace` | Request-level audit |
| `off` | Does not start a proxy for recording | OpenClaw is `null`; Codex/Claude Code derive from native | none kept | Minimal diagnostics-off mode |

After OpenClaw `off` or a degraded `actf`, SFT validation fails for missing tool
schema if the trajectory actually called tools. When there are no tool calls,
SFT `tools` is `[]`. Codex and Claude Code derive a schema from native actual
calls, so they do not depend on wire for this structural check, but the derived
schema is not a complete authoritative interface definition.

### 6.2 Thinking

| Flag | Current reply | Later request history | ACTF | SFT | raw |
| --- | --- | --- | --- | --- | --- |
| `--keep-thinking` (default) | Accept whatever the backend returns | Keep normal Harness history | Save visible reasoning | Save from ACTF | Keep original events |
| `--no-keep-thinking` | The current turn can still produce reasoning | OpenClaw strips later wire history; Codex/Claude Code do not rewrite resume/continue | Reasoning emptied | Reasoning emptied | Original events still kept |

`--thinking LEVEL` controls the logical grade sent to the Harness/model request.
It cannot invent visible text from nothing. See
[Thinking configuration](../harnesses/openclaw-configuration.md#thinking-system-prompt-and-wire-trace)
for GPT native effort, open-model profiles, and OpenClaw image limits, and
[Thinking and effort](../harnesses/claude-code-configuration.md#thinking-and-effort)
for the Claude Code mapping. When the backend reports only reasoning tokens and
does not return a reasoning summary/content, ACTF/SFT have no
`reasoning_content` to save. A GPT compacted summary enters that field as-is;
encrypted reasoning does not enter SFT.

Recommended paths by model class:

- GPT reasoning series (for example `gpt-5.6-sol`): use `external` /
  `external_always` to obtain explicit CoT;
- Claude series (for example Claude Opus 4.8, example ID `claude-opus-4-8`): use
  signature recovery;
- Open models such as Qwen and GLM that return plaintext reasoning: keep the
  default `--keep-thinking` so CoT goes directly into ACTF/SFT
  `reasoning_content`.

Even with `--wire-trace off`, `--no-keep-thinking` must still start a proxy to
strip request history, so OpenClaw still requires a proxyable `baseUrl` in
`--model-config`. Codex and Claude Code do not perform that history stripping.

`--external-thinking external|external_always` is a separate path: native
reasoning is forced off, explicit thoughts are stored as tool calls in raw/ACTF,
and they are folded back into `reasoning_content` in SFT.
`--no-keep-thinking` does not delete those explicit thoughts.

Claude Code currently supports only native thinking.
`external/external_always` is rejected before Docker starts.

### 6.3 Claude signature recovery

| Flag | Extra calls | ACTF/SFT reasoning | raw |
| --- | --- | --- | --- |
| `off` (default) | None | Native thinking/summary | Kept as-is |
| `fill` | Request only signed blocks whose native reasoning is empty | Fill empties on success; keep empty on failure | Kept as-is |
| `prefer` | Request every signed block | Replace the native summary on success; fall back to native on failure | Kept as-is |

Recovery is an independent call after the task finishes. It does not enter
`--continue` history. It requires `--keep-thinking` and is bounded by the
remaining `--timeout` budget of the rollout. Mode, attempt count, and max tokens
are written into the run lock. Resume cannot mix different recovery
configurations. Full semantics:
[Claude Code Signature recovery](../harnesses/claude-code-configuration.md#signature-recovery-experimental).

### 6.4 Cache trace (OpenClaw)

| Flag | ACTF/SFT system | `cache_trace.jsonl` |
| --- | --- | --- |
| `--cache-trace` (default) | Use the first captured assembled system prompt | Kept in the final sample only in full mode |
| `--no-cache-trace` | `""` | Not created |

If training needs the first assembled system prompt the Agent captured, do not
turn cache trace off. If you need to reproduce the context of every model call,
ACTF/SFT alone is not enough. You must also keep full cache/wire.

## 7. Success, failure, retry, and Resume

### 7.1 Success guarantees

An ordinary sample with status `ok` is guaranteed to have:

- `trace.jsonl`;
- `trace_ACTF.json`;
- `trace_SFT.json`;
- `info.json`.

A User Simulator sample also guarantees a root `conversation.json` and the
Simulator's own raw/ACTF/SFT. `results/`, cache trace, and wire trace are always
conditional artifacts. They are not required for success.

### 7.2 Failed samples

`error` or `timeout` does not mean the directory is empty. The finish phase
tries to recover raw, the system prompt, tools, and safe outputs, then attempts
ACTF/SFT conversion. A failed sample can therefore contain a complete or partial
trajectory. That is useful for diagnosis, but it does not enter run-level
`sft.jsonl`.

These cases turn an otherwise successful Harness result into `error`:

- No raw session trace found;
- ACTF or SFT conversion failed;
- Copying the task output directory failed;
- A symbolic link or special file was found in the output;
- Either User Simulator participant failed, or its trajectory conversion
  failed.

### 7.3 Retry

`--retries N` runs at most N extra complete rollouts. Before each attempt
starts, runtime raw/cache/wire files from the previous attempt are cleaned so
attempts are not mixed. Sample `info.json.attempts` keeps each status and
elapsed time. Canonical raw/ACTF/SFT correspond to the final attempt.

### 7.4 Resume

Resume first requires the current run lock to match the old `lock.json`. Changes
to task/model order, the Task Bundle, model configuration, Docker image ID,
timeout, or trajectory-semantic options stop immediately so different collection
conditions are not mixed in one batch. If the old run has no lock, the current
lock is written and every old sample is rerun once. A durable migration marker
keeps reuse forbidden until the full recapture and run-level SFT aggregation
succeed, so leftover samples are not trusted even if the process is killed
midway.

After the lock matches, `--resume` reuses only old samples whose status was `ok`
and that satisfy all of the following:

- Sample identity matches the current request;
- raw contains a valid message, and the ACTF schema / contiguous steps are
  valid;
- SFT is identical to the canonical projection regenerated from ACTF;
- `full` mode requires the wire trace to exist, and also requires the cache
  trace when cache trace is enabled;
- The current Task Bundle digest matches the old provenance;
- Context did not use `capture=none`;
- Output file paths are safe, the files exist, and their sizes are unchanged;
- A User Simulator task also has a valid `conversation.json`, participant info,
  and Simulator raw/ACTF/SFT.

A reused result is marked `skipped` in this run and is still written into the
new `sft.jsonl`. If any required file is missing or trajectory validation fails,
the entire sample is rerun. SHA-256 values stored in the output manifest are
not recomputed on Resume, so a routine checkpoint restart does not reread large
artifacts. Re-check that hash separately when you need a content-level audit.

## 8. Recommended configurations

### Batch-generate text/tool SFT

```bash
roller collect \
  --tasks tasks.jsonl \
  --model provider/model \
  --model-config models.json \
  --thinking high \
  --wire-trace actf \
  --output runs
```

Keep the default `--cache-trace` and `--keep-thinking`. This configuration
produces complete ACTF/SFT and deletes the larger diagnostic intermediate layer.

### Debug reasoning, compaction, or request history

```bash
roller collect \
  --tasks tasks.jsonl \
  --model provider/model \
  --model-config models.json \
  --thinking high \
  --wire-trace full \
  --output debug-runs
```

Use this configuration to confirm the messages/input that OpenClaw actually
hands to the model API after context assembly, compaction, pruning, and provider
adaptation. After debugging, prefer to keep raw/ACTF/SFT. Delete full cache/wire
once request-level audit is no longer needed, to reduce duplicated data and
sensitive-information exposure.

### Fill missing Claude Code reasoning

```bash
roller collect \
  --harness claude-code \
  --tasks tasks.jsonl \
  --model claude-opus-4-8 \
  --api-base http://your-api.example/v1/messages \
  --api-key-env ROLLER_ANTHROPIC_KEY \
  --thinking high \
  --claude-signature-recovery fill \
  --claude-signature-recovery-attempts 2 \
  --timeout 1200 \
  --output recovered-runs
```

For batch training, start with `fill`. Use `prefer` only when you explicitly
accept rewrite risk and will audit `signature_recovery.jsonl.quality`.

## 9. Checks before publishing a training set

Check at least the following:

1. Run `info.json.summary.failed` is 0, or failed tasks have been explicitly
   excluded;
2. The number of lines in `sft.jsonl` equals the expected `ok + skipped`
   task/model request count;
3. Each line has only `messages` and `tools` at the top level;
4. Every assistant has `reasoning_content`, `content`, and `tool_calls`;
5. Tool call IDs and tool messages correspond one to one;
6. The tools schema is compatible with the tool format of the chat template used
   at training time;
7. System prompts, user content, tool results, reasoning, and Agent outputs have
   been reviewed for privacy;
8. If you used `--no-keep-thinking`, confirm that the raw/full diagnostic layer
   will not be mistaken for thinking-stripped published data;
9. If the dataset claims to reproduce model-visible context, confirm collection
   used `--wire-trace full`, wire `seq` is contiguous, and the request count
   matches expectations. Do not substitute ACTF/SFT for per-request evidence;
10. If you used Claude signature recovery, filter `status!=ok`, sample-check
    `rewrite_or_unverified/unverified`, and account for the extra API cost
    separately.

Hermes raw uses `roller.hermes-events/v1`: full request snapshots, model responses/usage, and tool messages recorded before context compression. It produces no `cache_trace.jsonl`. See [Hermes traces](../harnesses/hermes-configuration.md).
