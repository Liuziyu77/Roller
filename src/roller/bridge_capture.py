"""Native adapters for Roller's pinned Python/SDK bridges.

Identity is supplied by each executing agent, never inferred from prompt text.
Pi and DeepSeek's bundled profiles have no delegation tool or extensions.
"""
from pathlib import Path
from typing import Any

from roller.capture import read_jsonl, native_directory
from roller.capture_protocol import NativeCapture


def read_bridge_native(directory: Path, harness: str) -> NativeCapture:
    native = NativeCapture(harness, "openai-chat-completions", capabilities={
        "request_response": "full", "identity": "native_session",
        "native_output_coverage": "message_id" if harness == "hermes" else "assistant_count",
        "subagents": "native_delegate_task" if harness == "hermes" else "not_available_in_bundled_profile",
        "background_drain": False,
    })
    tool_results = set()
    children = []
    for path in sorted(native_directory(directory).glob("*.jsonl")):
        rows = read_jsonl(path)
        metadata = next((r for r in rows if r.get("type") in {"metadata", "participant"}), {})
        sid = metadata.get("session_id")
        if not isinstance(sid, str) or not sid:
            native.errors.append(f"{path.name}: missing native session identity")
            continue
        pid = "session:" + sid
        source = path.relative_to(directory).as_posix()
        parent = metadata.get("parent_session_id")
        native.participants[pid] = {"participant_id": pid, "native_session_id": sid,
            "role": "child" if parent else "root", "identity_status": "observed",
            "native_files": [source], "call_ids": []}
        native.session_owners.setdefault(sid, set()).add(pid)
        count = 0
        for index, row in enumerate(rows):
            if row.get("type") == "runner_error":
                native.errors.append(f"{path.name}: native runner error")
            if harness == "hermes":
                if row.get("type") == "response":
                    mid = row.get("response", {}).get("id")
                    if isinstance(mid, str):
                        native.response_owners.setdefault(mid, set()).add(pid)
                if row.get("type") == "message" and row.get("message", {}).get("role") == "tool":
                    tool_results.add((sid, row["message"].get("tool_call_id")))
            elif harness == "pi":
                event = row.get("event", {})
                if event.get("type") == "message_end" and event.get("message", {}).get("role") == "assistant":
                    count += event["message"].get("stopReason") in {"stop", "toolUse"}
            else:
                event = row.get("payload", {}).get("event", {})
                if row.get("method") == "session.event" and event.get("type") == "assistant/message":
                    count += not event.get("data", {}).get("interrupted", False)
        if harness != "hermes":
            native.expected_call_counts[pid] = count
        if parent:
            children.append((pid, metadata, rows, source))
    for pid, metadata, rows, source in children:
        parent = "session:" + metadata["parent_session_id"]
        tool_id = metadata.get("tool_call_id")
        base: dict[str, Any] = {"parent_participant_id": parent, "child_participant_id": pid,
            "tool_call_id": tool_id, "status": "observed" if tool_id and parent in native.participants else "unknown",
            "evidence": [{"path": source, "event_id": "participant"}]}
        native.relations.append({**base, "type": "spawn"})
        end = next((r for r in reversed(rows) if r.get("type") == "child_end"), None)
        if end is not None:
            native.relations.append({**base, "type": "child_completed" if end.get("completed") else "child_failed"})
        else:
            native.background_detected = True
        if (metadata["parent_session_id"], tool_id) in tool_results and end is not None:
            native.relations.append({**base, "type": "result_delivered"})
    return native
