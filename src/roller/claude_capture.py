"""Claude native evidence and Anthropic responses mapped to capture records."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from roller.capture import read_jsonl, native_directory
from roller.capture_protocol import NativeCapture, finalize_capture
from roller.model_protocols import anthropic_response


def _participant(event: dict[str, Any], source: str) -> str:
    if event.get("agentId"):
        return "agent:" + str(event["agentId"])
    if event.get("isSidechain"):
        # Source identity is distinct from a confirmed native agent identity.
        return "unknown:" + source.removeprefix("raw/")
    return "root:" + str(event.get("sessionId") or "unknown")


def read_claude_native(directory: Path) -> NativeCapture:
    """Translate Claude native identities and tool evidence into the common contract."""
    participants: dict[str, dict[str, Any]] = {}
    native: list[tuple[str, dict[str, Any], str]] = []
    ids: dict[str, set[str]] = {}
    for path in sorted(native_directory(directory).glob("*.jsonl")):
        source = path.relative_to(directory).as_posix()
        for event in read_jsonl(path):
            if event.get("type") not in {"user", "assistant"}:
                continue
            pid = _participant(event, source)
            part = participants.setdefault(pid, {"participant_id": pid,
                "native_session_id": event.get("sessionId"),
                "native_agent_id": event.get("agentId"),
                "role": "child" if event.get("agentId") or event.get("isSidechain") else "root",
                "identity_status": "observed" if event.get("agentId") or (event.get("sessionId") and not event.get("isSidechain")) else "unknown",
                "native_files": [], "call_ids": []})
            if source not in part["native_files"]:
                part["native_files"].append(source)
            native.append((pid, event, source))
            message = event.get("message", {})
            if event.get("type") == "assistant" and message.get("model") != "<synthetic>":
                message_id = message.get("id")
                if isinstance(message_id, str):
                    ids.setdefault(message_id, set()).add(pid)

    relations = []
    tools: dict[str, tuple[str, dict[str, Any], str]] = {}
    for pid, event, source in native:
        content = event.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Agent":
                tools[block["id"]] = (pid, event, source)
    seen_results = set()
    for pid, event, source in native:
        result = event.get("toolUseResult")
        if not isinstance(result, dict) or not result.get("agentId"):
            continue
        content = event.get("message", {}).get("content", [])
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_id = block.get("tool_use_id")
            if tool_id not in tools or tool_id in seen_results:
                continue
            seen_results.add(tool_id)
            parent, spawn, spawn_source = tools[tool_id]
            child = "agent:" + str(result["agentId"])
            if child not in participants:
                participants[child] = {"participant_id": child, "role": "child",
                    "identity_status": "observed",
                    "native_agent_id": result["agentId"], "native_session_id": None,
                    "native_files": [], "call_ids": []}
            base = {"parent_participant_id": parent, "child_participant_id": child,
                "tool_call_id": tool_id, "parent_native_message_id": spawn.get("message", {}).get("id"),
                "evidence": [{"path": spawn_source, "event_id": spawn.get("uuid")},
                             {"path": source, "event_id": event.get("uuid")}],
                "status": "observed"}
            relations.append({**base, "type": "spawn"})
            finished_child = result.get("status") == "completed"
            relations.append({**base, "type": "result_delivered" if finished_child else "launch_acknowledged", "native_status": result.get("status")})
            if finished_child:
                relations.append({**base, "type": "child_completed"})
    for tool_id, (pid, event, source) in tools.items():
        if tool_id not in seen_results:
            relations.append({"type": "spawn", "status": "unknown", "tool_call_id": tool_id,
                "parent_participant_id": pid, "child_participant_id": None,
                "evidence": [{"path": source, "event_id": event.get("uuid")}],
                "reason": "native Agent result did not identify a child"})

    background = any(event.get("message", {}).get("content") and any(
        isinstance(b, dict) and b.get("name") == "Agent" and b.get("input", {}).get("run_in_background")
        for b in event["message"]["content"] if isinstance(event["message"]["content"], list))
        for _, event, _ in native)
    return NativeCapture(harness="claude-code", protocol="anthropic-messages",
        participants=participants, response_owners=ids, relations=relations,
        background_detected=background,
        capabilities={"request_response": "full", "identity": "response/native message ID matching",
                      "native_output_coverage": "message_id", "relations": "native_tool_result",
                      "background_drain": False})


def finalize_claude_capture(directory: Path, *, execution_status: str,
                           recorder_error: str | None = None) -> dict[str, Any]:
    return finalize_capture(directory, read_claude_native(directory),
                            execution_status=execution_status, recorder_error=recorder_error)
