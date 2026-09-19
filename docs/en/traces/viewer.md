# Trajectory Viewer

Roller includes a read-only Web Viewer that needs no extra dependencies. It groups
a run by task and displays model rollouts, reasoning, tool calls, tool results,
and final replies. The Viewer reads existing Roller artifacts directly.
It does not generate a new trajectory copy, and it does not modify
`trace_SFT.json` or `sft.jsonl`.

## 1. Start

Point the command at a Roller run root that contains `info.json` or `samples/`:

```bash
roller view /path/to/runs/my-run
```

It listens on `127.0.0.1:8765` by default. The terminal prints the actual URL
and the locked run path:

```text
Roller Viewer: http://127.0.0.1:8765
Run directory: /path/to/runs/my-run
```

You can change the port:

```bash
roller view /path/to/runs/my-run --port 9000
```

The Viewer process only reads files and serves the page. It does not start
Docker, OpenClaw, or a model service. Stop it with `Ctrl-C`.

## 2. Access from a remote machine

For remote access, keep the Viewer on the loopback address and open an SSH tunnel
from your local machine:

```bash
ssh -L 8765:127.0.0.1:8765 user@remote-host
```

If you must expose the service directly:

```bash
roller view /path/to/run --host 0.0.0.0 --port 8765
```

The Viewer currently has no authentication. Listening on a public interface
exposes system prompts, user input, reasoning, tool arguments, tool results, and
run metadata. Use it only on a trusted network that already has access control.

## 3. Page layout

The page has three columns:

1. The left column lists tasks by `task_id`. Multiple model rollouts of the same
   task appear in the same group. You can search by task/model/error text and
   filter by status or model.
2. The middle column shows the ACTF timeline of the selected rollout. The step
   title first shows tokens, elapsed time, stop reason, and reasoning/tool
   counts. The full step is loaded only after you click it.
3. The right Inspector shows sample information, the first system prompt saved
   at the ACTF top level, the full tools schema, and the original `info.json`.
   A User Simulator task also shows `conversation.json`.

Reasoning uses its own region and is collapsed by default. Tool calls are paired
with their results by `tool_call_id`. Results with `isError=true` use an error
style. Everything on the page is rendered as text. HTML or scripts inside the
trajectory are not executed.

The page hash stores the rollout ID, so you can copy the current URL as a deep
link to that task in the same Viewer process.

## 4. Data sources

| Content | File | When loaded |
| --- | --- | --- |
| Run name, state, processed/total, total usage, collection config | Run `info.json` | Home page load/refresh |
| Task/model/status/error/path | Run and sample `info.json` | Home page load |
| Step summaries and system/tools | `trace_ACTF.json` | Rollout selected |
| Step reasoning/content/tool/result | `trace_ACTF.json` | Step expanded |
| Dialogue both sides can see | `conversation.json` | User Simulator rollout selected |

The Viewer does not use `trace_SFT.json` as its display source, because SFT
does not contain step ID, timing, metric, stop reason, and observation
metadata. `trace.jsonl`, `cache_trace.jsonl`, and `wire_trace.jsonl` are also
not sent to the browser by default.

What the Viewer shows is ACTF's analysis projection of session events, not the
full context of every model request. When OpenClaw applies compaction, pruning,
runtime/hook injection, or protocol adaptation between the session and the
provider call, the middle timeline cannot prove which history the model saw
verbatim. For that kind of audit, enable `--wire-trace full` at collection time
and inspect `wire_trace.jsonl` and `cache_trace.jsonl` in the sample offline.
See the limits in
[Trajectory output file guide](trajectory-outputs.md#32-session-history-is-not-the-model-request-context).

During collection, the Run card on the home page shows `state` and
`processed / total` from the latest checkpoint. Roller updates on the first
result, then after every additional 25 results, and also immediately when
completion events are at least 30 seconds apart. It force-updates on interrupt
and during finalization.
After Resume, the sample's on-disk `info.json` still keeps the original
`status=ok`. The Viewer overlays `status=skipped` from this run's index so the
Completed card can mark the reuse count accurately. The Thinking card shows the retention policy (kept/stripped) and configured native thinking level.

Until the run has finalized `info.json`, the Viewer also scans
`samples/<model>/<task>/info.json`, so samples finalized after a checkpoint
still appear. In-progress temporary directories without a sample `info.json` do
not enter the list. Click **Refresh** in the top-right to reread state and
samples.

## 5. Lazy loading and large content

The home page does not read every ACTF. The server parses the matching
`trace_ACTF.json` only when the user selects a rollout, and keeps a limited
number of recent trajectories in memory.

When a step is expanded, each user/reasoning/content/arguments/tool result
field returns at most 40,000 characters by default. A truncated region shows
the original length and a "Load more" button. A further load is capped at
1,000,000 characters. This limit affects only the browser response. It does not
modify files on disk.

## 6. Missing and failed traces

- Samples with `status=error/timeout` still appear in the list. If ACTF already
  exists, you can browse the partial trajectory from before the failure.
- Without ACTF, the page still shows sample status, error, and metadata, and
  the center shows `NO ACTF`.
- When no cache trace was captured, the system prompt is shown as empty.
- When no wire tools were captured, the tools panel is shown as empty. This
  does not affect display of tool call/result in existing steps.
- ACTF reasoning produced with `--no-keep-thinking` is empty. The page does not
  fabricate content or infer it back from SFT.

## 7. Read-only and path safety

`roller view` pins one run directory as the only root it is allowed to read.
The API uses an internal rollout ID and does not accept an arbitrary host
directory from the browser. Sample/ACTF paths in run `info.json` that escape
the root are rejected. The static server does not provide directory listing,
file writes, command execution, or an arbitrary artifact-download API.

Browser responses use security headers that block cross-origin reads, MIME
sniffing, and execution of inline/external scripts. These limits are not a
substitute for network authentication, so a sensitive run should still be
reached only through loopback or an SSH tunnel.

## 8. Common questions

### Newly finished tasks do not appear on the page

Click **Refresh** in the top-right. The Viewer does not continuously poll the
disk, so a large run does not generate extra I/O.

### Port already in use

Choose another port:

```bash
roller view /path/to/run --port 8766
```

### The Viewer reports this is not a Roller run

The input path must be the run root, not `samples/`, a single sample, or
`trace_ACTF.json`. A valid root contains run `info.json` or `samples/`.
