"""Claude native evidence and Anthropic responses mapped to capture records."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET
import re

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
    event_index: dict[str, dict[str, Any]] = {}
    for path in sorted(native_directory(directory).glob("*.jsonl")):
        source = path.relative_to(directory).as_posix()
        for event in read_jsonl(path):
            if event.get("uuid"):
                event_index[event["uuid"]] = event
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
        content = event.get("message", {}).get("content", [])
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_id = block.get("tool_use_id")
            if tool_id not in tools or tool_id in seen_results:
                continue
            parent, spawn, spawn_source = tools[tool_id]
            result = event.get("toolUseResult")
            if not isinstance(result, dict) or not result.get("agentId"):
                # Nested Agent results in 2.1.222 omit toolUseResult. Their
                # native final metadata block still carries the child ID.
                blocks = block.get("content", [])
                metadata = blocks[-1].get("text", "") if isinstance(blocks, list) and blocks and isinstance(blocks[-1], dict) else ""
                match = re.fullmatch(r"agentId: ([a-z0-9]+) \(use SendMessage with to: '([a-z0-9]+)', summary: '[^']*' to continue this agent\)\n<usage>.*</usage>", metadata, re.DOTALL)
                if (not match or match[1] != match[2] or pid != parent
                        or event.get("sourceToolAssistantUUID") != spawn.get("uuid")
                        or "agent:" + match[1] not in participants):
                    continue
                result = {"agentId": match[1], "status": "failed" if block.get("is_error") else "completed"}
            seen_results.add(tool_id)
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
            terminal = result.get("status") in {"completed", "failed", "error"}
            relations.append({**base, "type": "result_delivered" if terminal else "launch_acknowledged", "native_status": result.get("status")})
            if terminal:
                relations.append({**base, "type": "child_completed" if result["status"] == "completed" else "child_failed"})
    # Pinned Claude -p keeps its native loop alive for background completion.
    # Parse only harness-originated notifications and verify both native IDs.
    for pid, event, source in native:
        if event.get("origin", {}).get("kind") != "task-notification":
            continue
        text = event.get("message", {}).get("content")
        if not isinstance(text, str):
            continue
        try:
            notice = ET.fromstring(text)
        except ET.ParseError:
            continue
        if notice.tag != "task-notification":
            continue
        tool_id = notice.findtext("tool-use-id")
        child = "agent:" + (notice.findtext("task-id") or "")
        launch = next((r for r in relations if r["type"] == "spawn" and r["tool_call_id"] == tool_id
                       and r["parent_participant_id"] == pid and r["child_participant_id"] == child), None)
        status = notice.findtext("status")
        if launch is None or status not in {"completed", "failed", "error"}:
            continue
        base = {**launch, "evidence": [*launch["evidence"], {"path": source, "event_id": event.get("uuid")}],
                "native_status": status}
        relations.append({**base, "type": "child_completed" if status == "completed" else "child_failed"})
        response_ids = set()
        for owner, following, _ in native:
            if owner != pid or following.get("type") != "assistant":
                continue
            previous = following.get("parentUuid")
            visited = set()
            while previous in event_index and previous not in visited:
                if previous == event.get("uuid"):
                    mid = following.get("message", {}).get("id")
                    if mid in ids and pid in ids[mid]:
                        response_ids.add(mid)
                    break
                visited.add(previous)
                prior = event_index[previous]
                if prior.get("type") in {"user", "assistant"}:
                    break
                previous = prior.get("parentUuid")
        # Delivery is independently observed even without a following request.
        if response_ids:
            for mid in sorted(response_ids):
                relations.append({**base, "type": "result_delivered", "delivery_mode": "notification",
                                  "context_message": text, "context_response_id": mid})
        else:
            relations.append({**base, "type": "result_delivered", "delivery_mode": "notification"})
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
    # A resumed child can be launched again with a new tool ID. An earlier
    # completion of the same participant does not close its new execution.
    ended = {(r.get("child_participant_id"), r.get("tool_call_id")) for r in relations
             if r["type"] in {"child_completed", "child_failed"}}
    pending = sorted({r["child_participant_id"] for r in relations
                      if r["type"] == "spawn" and r.get("child_participant_id")
                      and (r["child_participant_id"], r.get("tool_call_id")) not in ended})
    return NativeCapture(harness="claude-code", protocol="anthropic-messages",
        participants=participants, response_owners=ids, relations=relations,
        background_detected=background,
        lifecycle={"status": "unknown" if any(r["type"] == "spawn" and r.get("status") == "unknown" for r in relations)
                   else "pending" if pending else "complete", "pending_participant_ids": pending},
        capabilities={"request_response": "full", "identity": "response/native message ID matching",
                      "native_output_coverage": "message_id", "relations": "native_tool_result",
                      "background_drain": True})


def finalize_claude_capture(directory: Path, *, execution_status: str,
                           recorder_error: str | None = None, debug_retention: str = "full") -> dict[str, Any]:
    return finalize_capture(directory, read_claude_native(directory),
                            execution_status=execution_status, recorder_error=recorder_error, debug_retention=debug_retention)
