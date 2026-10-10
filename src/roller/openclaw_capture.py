"""OpenClaw session identities and native sessions_spawn evidence."""
import json
from pathlib import Path

from roller.capture import read_jsonl, native_directory
from roller.capture_protocol import NativeCapture


def read_openclaw_native(directory: Path) -> NativeCapture:
    native = NativeCapture("openclaw", "openai-chat-completions", capabilities={
        "request_response": "full", "identity": "native_session_header",
        "native_output_coverage": "stream_context_count", "subagents": "native_sessions_spawn",
        "background_drain": True,
    })
    cache = native_directory(directory) / "stream-contexts.jsonl"
    if not cache.exists():
        cache = native_directory(directory) / "cache_trace.jsonl"
    cache_source = cache.relative_to(directory).as_posix()
    cache_rows = read_jsonl(cache) if cache.exists() else []
    registry = native_directory(directory) / "subagent-runs.json"
    runs = json.loads(registry.read_text()) if registry.exists() else []
    runs = {r["runId"]: r for r in runs}
    alias_path = native_directory(directory) / "tool-id-aliases.jsonl"
    aliases = {}
    if alias_path.exists():
        for row in read_jsonl(alias_path):
            aliases.setdefault(row["original_id"], set()).add(row["context_id"])
    keys = {}
    counts = {}
    if cache.exists():
        for row in cache_rows:
            sid = row.get("sessionId")
            if not isinstance(sid, str):
                continue
            pid = "session:" + sid
            key = row.get("sessionKey", "")
            if key:
                keys[key] = pid
            native.participants.setdefault(pid, {"participant_id": pid, "native_session_id": sid,
                "native_session_key": key, "role": "child" if ":subagent:" in key else "root",
                "identity_status": "observed", "native_files": [cache_source], "call_ids": []})
            native.session_owners.setdefault(sid, set()).add(pid)
            if row.get("stage") == "stream:context":
                counts[pid] = counts.get(pid, 0) + 1
    native.expected_call_counts = counts
    messages = {}
    for path in sorted((native_directory(directory) / "sessions").rglob("*.jsonl")):
        rows = read_jsonl(path)
        sid = next((r.get("id") for r in rows if r.get("type") == "session"), None)
        pid = "session:" + str(sid)
        if pid not in native.participants:
            continue
        source = path.relative_to(directory).as_posix()
        native.participants[pid]["native_files"].append(source)
        messages[pid] = [(r, source) for r in rows if r.get("type") == "message"]
    for parent, rows in messages.items():
        for row, source in rows:
            message = row.get("message", {})
            if message.get("role") != "toolResult" or message.get("toolName") != "sessions_spawn":
                continue
            result = message.get("details")
            if not isinstance(result, dict):
                text = "".join(b.get("text", "") for b in message.get("content", []) if isinstance(b, dict))
                try:
                    result = json.loads(text)
                except ValueError:
                    continue
            if result.get("status") != "accepted":
                continue
            child = keys.get(result.get("childSessionKey"))
            base = {"parent_participant_id": parent, "child_participant_id": child,
                "tool_call_id": message.get("toolCallId"), "status": "observed" if child else "unknown",
                "evidence": [{"path": source, "event_id": row.get("id")}],
                "native_run_id": result.get("runId")}
            # The pinned pi-ai Responses bridge encodes a native tool ID as
            # call_id|item_id. Keep the native ID and require BOTH provider
            # references to match the captured model output; never match text.
            tool_id = message.get("toolCallId", "")
            if isinstance(tool_id, str) and tool_id.count("|") == 1:
                call_id, item_id = tool_id.split("|")
                if call_id and item_id:
                    base["model_tool_ref"] = {"protocol": "openai-responses",
                                              "call_id": call_id, "item_id": item_id}
            mapped = aliases.get(message.get("toolCallId"), set())
            if len(mapped) == 1:
                base["context_tool_call_id"] = next(iter(mapped))
                base["evidence"].append({"path": "native/tool-id-aliases.jsonl", "event_id": message.get("toolCallId")})
            native.relations.extend([{**base, "type": kind} for kind in ("spawn", "launch_acknowledged")])
            child_rows = messages.get(child, [])
            end = next((r for r, _ in reversed(child_rows) if r.get("message", {}).get("role") == "assistant"), None)
            run = runs.get(result.get("runId"), {})
            failed = run.get("endedAt") and run.get("outcome", {}).get("status") in {"error", "timeout"}
            if failed:
                native.relations.append({**base, "type": "child_failed",
                    "evidence": [*base["evidence"], {"path": "native/subagent-runs.json", "event_id": result.get("runId")}]})
            elif end and end["message"].get("stopReason") in {"stop", "end_turn"}:
                native.relations.append({**base, "type": "child_completed"})
            else:
                native.background_detected = True
            if run.get("delivery", {}).get("status") == "delivered":
                announce = f"announce:v1:{result.get('childSessionKey')}:{result.get('runId')}"
                context_message = None
                for context in cache_rows:
                    if context.get("runId") != announce or context.get("stage") != "stream:context":
                        continue
                    for message in context.get("messages", []):
                        text = message.get("content")
                        if isinstance(text, list):
                            text = "\n".join(b.get("text", "") for b in text if isinstance(b, dict))
                        if message.get("role") == "user" and isinstance(text, str) and f"session_key: {result.get('childSessionKey')}\n" in text:
                            context_message = text
                            break
                    if context_message:
                        break
                native.relations.append({**base, "type": "result_delivered", "delivery_mode": "notification",
                    "context_message": context_message,
                    "evidence": [*base["evidence"], {"path": "native/subagent-runs.json", "event_id": result.get("runId")},
                                 {"path": cache_source, "event_id": announce}]})
    for pid in native.participants:
        if pid not in messages:
            native.errors.append(f"native transcript missing for {pid}")
    if not native.participants:
        native.errors.append("no OpenClaw native session evidence")
    return native
