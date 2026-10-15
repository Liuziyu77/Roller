"""OpenCode native session/task evidence translated to the capture contract."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from roller.capture import read_jsonl, native_directory
from roller.capture_protocol import NativeCapture, finalize_native_capture
from roller.opencode_actf import native_messages


def read_opencode_native(directory: Path) -> NativeCapture:
    native = NativeCapture(harness="opencode", protocol="openai-chat-completions",
        capabilities={"request_response": "full", "identity": "native session header matched to journal",
                      "native_output_coverage": "completed_message_count", "relations": "native_task_metadata",
                      "background_drain": False})
    events = []
    sessions: dict[str, dict[str, Any]] = {}
    sources: dict[str, list[str]] = {}
    for path in sorted(native_directory(directory).glob("*.jsonl")):
        source = path.relative_to(directory).as_posix()
        rows = read_jsonl(path)
        events.extend(rows)
        for row in rows:
            if row.get("type") == "capture_error":
                native.errors.append(str(row.get("error") or "native capture error"))
            info = None
            if row.get("type") == "session_export":
                info = row.get("data", {}).get("info")
            elif row.get("type") == "event" and row.get("event", {}).get("type") in {"session.created", "session.updated"}:
                info = row["event"].get("properties", {}).get("info")
            if isinstance(info, dict) and info.get("id"):
                sessions[info["id"]] = {**sessions.get(info["id"], {}), **info}
                sources.setdefault(info["id"], []).append(source)
            if row.get("type") == "event" and row.get("event", {}).get("type") in {"message.updated", "message.part.updated"}:
                prop = row["event"].get("properties", {})
                sid = (prop.get("info") or prop.get("part") or {}).get("sessionID")
                if sid:
                    sources.setdefault(sid, []).append(source)
    messages = native_messages(events)
    for message in messages:
        sid = message["info"].get("sessionID")
        if sid and message["info"].get("error"):
            native.errors.append("native assistant failed in session " + sid)
    for message in messages:
        sid = message["info"].get("sessionID")
        if not sid:
            continue
        for part in message["parts"]:
            if part.get("type") == "tool" and part.get("tool") == "task":
                child = part.get("state", {}).get("metadata", {}).get("sessionId")
                if child:
                    sessions[child] = {**sessions.get(child, {}), "id": child, "parentID": sid}
    def participant(sid):
        return ("agent:" if sessions.get(sid, {}).get("parentID") else "root:") + sid
    for sid, info in sessions.items():
        pid = participant(sid)
        native.participants[pid] = {"participant_id": pid,
            "identity_status": "observed",
            "role": "child" if info.get("parentID") else "root",
            "native_session_id": sid, "native_agent_id": info.get("agent"),
            "native_files": sorted(set(sources.get(sid, []))), "call_ids": []}
        native.session_owners[sid] = {pid}
        native.expected_call_counts[pid] = sum(m["info"].get("sessionID") == sid and
            m["info"].get("role") == "assistant" and bool(m["info"].get("time", {}).get("completed")) and
            m["info"].get("finish") in {"stop", "end-turn", "tool-calls"} and not m["info"].get("error") for m in messages)
    for message in messages:
        sid = message["info"].get("sessionID")
        if sid not in sessions:
            continue
        for part in message["parts"]:
            if part.get("type") != "tool" or part.get("tool") != "task":
                continue
            state = part.get("state", {})
            metadata = state.get("metadata", {})
            child = metadata.get("sessionId")
            background = bool(metadata.get("background") or state.get("input", {}).get("background"))
            native.background_detected |= background
            base = {"parent_participant_id": participant(sid),
                "child_participant_id": participant(child) if child in sessions else None,
                "tool_call_id": part.get("callID"),
                "evidence": [{"path": p, "event_id": part["id"]} for p in sorted(set(sources.get(sid, [])))],
                "status": "observed" if child in sessions else "unknown"}
            native.relations.append({**base, "type": "spawn"})
            if state.get("status") in {"completed", "error"}:
                native.relations.append({**base, "type": "launch_acknowledged" if background else "result_delivered", "native_status": state["status"]})
                if not background:
                    native.relations.append({**base, "type": "child_completed" if state["status"] == "completed" else "child_failed"})
    return native


def finalize_opencode_capture(directory: Path, *, execution_status: str,
                            recorder_error: str | None = None, debug_retention: str = "full") -> dict[str, Any]:
    return finalize_native_capture(directory, read_opencode_native,
        execution_status=execution_status, recorder_error=recorder_error,
        debug_retention=debug_retention)
