"""Claude native evidence and Anthropic responses mapped to capture records."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from roller.capture import read_jsonl
from roller.storage import atomic_write_json, write_jsonl


def anthropic_response(payload: bytes, content_type: str) -> dict[str, Any]:
    """Reassemble Messages SSE without reserializing the raw capture."""
    if "text/event-stream" not in content_type:
        message = json.loads(payload)
        if not isinstance(message, dict) or message.get("type") != "message":
            raise ValueError("response is not an Anthropic message")
        return message
    message: dict[str, Any] | None = None
    blocks: dict[int, dict[str, Any]] = {}
    inputs: dict[int, str] = {}
    stopped = False
    for frame in payload.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in frame.split("\n") if line.startswith("data:"))
        if not data:
            continue
        event = json.loads(data)
        kind = event.get("type")
        if kind == "error":
            raise ValueError("Anthropic stream returned an error")
        if kind == "message_start":
            message = event["message"]
            if not isinstance(message, dict):
                raise ValueError("invalid message_start")
            blocks = {i: b for i, b in enumerate(message.get("content", []))}
        elif kind == "content_block_start":
            blocks[event["index"]] = event["content_block"]
        elif kind == "content_block_delta":
            index = event["index"]
            block = blocks[index]
            delta = event["delta"]
            field = {"text_delta": "text", "thinking_delta": "thinking",
                     "signature_delta": "signature"}.get(delta.get("type"))
            if field:
                block[field] = block.get(field, "") + delta.get(field, "")
            elif delta.get("type") == "input_json_delta":
                inputs[index] = inputs.get(index, "") + delta.get("partial_json", "")
            else:
                raise ValueError(f"unsupported content delta: {delta.get('type')}")
        elif kind == "message_delta" and message is not None:
            message.update(event.get("delta", {}))
            message.setdefault("usage", {}).update(event.get("usage", {}))
        elif kind == "message_stop":
            stopped = True
    if message is None or not stopped:
        raise ValueError("Anthropic stream lacks message_start/message_stop")
    for index, text in inputs.items():
        blocks[index]["input"] = json.loads(text)
    message["content"] = [blocks[i] for i in sorted(blocks)]
    return message


def _participant(event: dict[str, Any], source: str) -> str:
    if event.get("agentId"):
        return "agent:" + str(event["agentId"])
    if event.get("isSidechain"):
        # Source identity is distinct from a confirmed native agent identity.
        return "unknown:" + source
    return "root:" + str(event.get("sessionId") or "unknown")


def finalize_claude_capture(directory: Path, *, execution_status: str,
                           recorder_error: str | None = None) -> dict[str, Any]:
    """Publish one attempt's calls, participants, relations and file hashes."""
    participants: dict[str, dict[str, Any]] = {}
    native: list[tuple[str, dict[str, Any], str]] = []
    ids: dict[str, set[str]] = {}
    for path in sorted((directory / "native").glob("*.jsonl")):
        source = path.relative_to(directory).as_posix()
        for event in read_jsonl(path):
            if event.get("type") not in {"user", "assistant"}:
                continue
            pid = _participant(event, source)
            part = participants.setdefault(pid, {"participant_id": pid,
                "native_session_id": event.get("sessionId"),
                "native_agent_id": event.get("agentId"),
                "role": "child" if event.get("agentId") or event.get("isSidechain") else "root",
                "native_files": [], "call_ids": []})
            if source not in part["native_files"]:
                part["native_files"].append(source)
            native.append((pid, event, source))
            message = event.get("message", {})
            if event.get("type") == "assistant" and message.get("model") != "<synthetic>":
                message_id = message.get("id")
                if isinstance(message_id, str):
                    ids.setdefault(message_id, set()).add(pid)

    events = read_jsonl(directory / "events.jsonl") if (directory / "events.jsonl").exists() else []
    requests = {e["request_seq"]: e for e in events if e.get("type") == "request"}
    responses = {e["request_seq"]: e for e in events if e.get("type") == "response"}
    calls = []
    by_message: dict[str, str] = {}
    for seq, event in sorted(requests.items()):
        completion = responses.get(seq, {})
        output = None
        errors = []
        response_path = completion.get("response_path")
        if not completion.get("complete") or completion.get("error"):
            errors.append(completion.get("error") or "response capture incomplete")
        try:
            if completion.get("content_encoding") not in {"", "identity"}:
                raise ValueError("encoded response is preserved raw but not parsed")
            if not response_path:
                raise ValueError("response is missing")
            output = anthropic_response((directory / response_path).read_bytes(), completion.get("content_type", ""))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(str(exc))
        matches = ids.get(output.get("id"), set()) if output else set()
        pid = next(iter(matches)) if len(matches) == 1 else None
        if pid is None:
            errors.append("response ID has no unique native participant")
        elif pid.startswith("unknown:") or pid == "root:unknown":
            errors.append("native participant identity is unresolved")
        if completion.get("http_status") != 200:
            errors.append("model HTTP request did not succeed")
        if output and output.get("stop_reason") not in {"end_turn", "tool_use", "stop_sequence"}:
            errors.append("model response did not finish normally")
        if output and output.get("model") != event["body"].get("model"):
            errors.append("response model does not match request")
        call = {"schema_version": "roller.call/v1", "call_id": event["call_id"],
            "attempt_id": directory.name, "request_seq": seq,
            "participant_id": pid, "protocol": "anthropic-messages",
            "output_source": "model", "started_at": event.get("timestamp"),
            "finished_at": completion.get("timestamp"),
            "request_path": event["path"], "context": event["body"], "output": output,
            "http_status": completion.get("http_status"), "response_path": response_path,
            "response_capture_status": "complete" if completion.get("complete") and not completion.get("error") else "partial",
            "identity_status": "observed" if pid and not pid.startswith("unknown:") and pid != "root:unknown" else "unknown",
            "execution_status": "completed" if output and completion.get("http_status") == 200 and output.get("stop_reason") in {"end_turn", "tool_use", "stop_sequence"} else "failed",
            "capture_status": "complete" if not errors else "partial",
            "export_status": "available" if not errors else "unavailable", "errors": errors}
        calls.append(call)
        if pid:
            participants[pid]["call_ids"].append(call["call_id"])
        if output and isinstance(output.get("id"), str):
            by_message[output["id"]] = call["call_id"]

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
                    "native_agent_id": result["agentId"], "native_session_id": None,
                    "native_files": [], "call_ids": []}
            base = {"parent_participant_id": parent, "child_participant_id": child,
                "tool_call_id": tool_id, "parent_call_id": by_message.get(spawn.get("message", {}).get("id")),
                "evidence": [{"path": spawn_source, "event_id": spawn.get("uuid")},
                             {"path": source, "event_id": event.get("uuid")}],
                "status": "observed"}
            relations.append({**base, "type": "spawn"})
            finished_child = result.get("status") == "completed"
            relations.append({**base, "type": "result_delivered" if finished_child else "launch_acknowledged", "native_status": result.get("status")})
            if finished_child:
                relations.append({**base, "type": "child_completed"})
            for call in calls:
                if call["participant_id"] != parent:
                    continue
                messages = call["context"].get("messages", [])
                if any(isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") == tool_id
                       for m in messages for b in (m.get("content", []) if isinstance(m.get("content"), list) else [])):
                    relations.append({**base, "type": "result_in_context" if finished_child else "launch_ack_in_context", "call_id": call["call_id"]})
    for tool_id, (pid, event, source) in tools.items():
        if tool_id not in seen_results:
            relations.append({"type": "spawn", "status": "unknown", "tool_call_id": tool_id,
                "parent_participant_id": pid, "child_participant_id": None,
                "evidence": [{"path": source, "event_id": event.get("uuid")}],
                "reason": "native Agent result did not identify a child"})

    for index, relation in enumerate(relations):
        relation["schema_version"] = "roller.relation/v1"
        relation["relation_id"] = f"{directory.name}:relation-{index:08d}"
    write_jsonl(directory / "calls.jsonl", calls)
    write_jsonl(directory / "relations.jsonl", relations)
    for pid, part in participants.items():
        relative = "participants/" + hashlib.sha256(pid.encode()).hexdigest()[:16] + "/calls.jsonl"
        write_jsonl(directory / relative, [c for c in calls if c["participant_id"] == pid])
        part["calls_path"] = relative
        expected = {mid for mid, owners in ids.items() if pid in owners}
        captured = {c["output"]["id"] for c in calls if c["participant_id"] == pid and c["output"] and c["capture_status"] == "complete"}
        part["uncaptured_message_ids"] = sorted(expected - captured)
        part["capture_status"] = "complete" if part["native_files"] and expected and not (expected - captured) else "partial"
    files = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files.append({"path": path.relative_to(directory).as_posix(),
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    background = any(event.get("message", {}).get("content") and any(
        isinstance(b, dict) and b.get("name") == "Agent" and b.get("input", {}).get("run_in_background")
        for b in event["message"]["content"] if isinstance(event["message"]["content"], list))
        for _, event, _ in native)
    complete = bool(calls) and not recorder_error and not background and all(c["capture_status"] == "complete" for c in calls) and all(p["capture_status"] == "complete" for p in participants.values()) and all(r["status"] == "observed" for r in relations)
    manifest = {"schema_version": "roller.capture/v1", "attempt_id": directory.name,
        "execution_status": execution_status, "capture_status": "complete" if complete else "partial",
        "recorder_error": recorder_error, "participants": list(participants.values()),
        "background_detected": background,
        "call_count": len(calls), "relation_count": len(relations), "files": files,
        "coverage": {"request_response": "observed HTTP Messages calls only",
                     "identity": "response/native message ID matching",
                     "lifecycle": "foreground native evidence; background drain not supported"}}
    atomic_write_json(directory / "manifest.json", manifest)
    return manifest
