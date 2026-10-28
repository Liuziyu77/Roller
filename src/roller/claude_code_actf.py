"""Projection of Claude Code native session JSONL into Roller ACTF."""

from __future__ import annotations

import base64
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from roller.storage import atomic_write_json

CLAUDE_COMPACTION_SFT_REASON = (
    "Claude context compaction cannot be represented by the linear ACTF projection; "
    "use captured per-call contexts instead"
)


def _read_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


def _event_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp(value: Any) -> str | None:
    parsed = _event_time(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S.%f") if parsed is not None else None


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return "" if value is None else _stringify(value)
    parts: list[str] = []
    for block in value:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif block is not None:
            parts.append(_stringify(block))
    return "\n\n".join(part for part in parts if part)


def _openrouter_redacted_reasoning(data: Any) -> str | None:
    prefix = "openrouter.reasoning:"
    if not isinstance(data, str) or not data.startswith(prefix):
        return None
    try:
        decoded = base64.b64decode(data[len(prefix) :] + "==").decode(
            "utf-8", "replace"
        )
        payload = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return None
    text = payload.get("text") if isinstance(payload, Mapping) else None
    return text if isinstance(text, str) and text else None


def _assistant_parts(
    content: Any,
) -> tuple[str, str, list[dict[str, Any]]]:
    if isinstance(content, str):
        return content, "", []
    if not isinstance(content, list):
        return "", "", []

    visible: list[str] = []
    reasoning: list[str] = []
    calls: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, str):
            visible.append(block)
            continue
        if not isinstance(block, Mapping):
            continue
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            visible.append(block["text"])
            continue
        if block_type in {"thinking", "reasoning", "analysis"}:
            value = block.get("text", block.get("thinking", block.get("reasoning")))
            if isinstance(value, str) and value:
                reasoning.append(value)
            continue
        if block_type == "redacted_thinking":
            decoded = _openrouter_redacted_reasoning(block.get("data"))
            if decoded:
                reasoning.append(decoded)
            continue
        if block_type != "tool_use":
            continue
        call_id = block.get("id", block.get("tool_use_id"))
        name = block.get("name")
        if not isinstance(call_id, str) or not call_id:
            continue
        if not isinstance(name, str) or not name:
            continue
        arguments = block.get("input", {})
        if not isinstance(arguments, dict):
            arguments = {"input": deepcopy(arguments)}
        calls.append(
            {
                "type": "toolCall",
                "id": call_id,
                "name": name,
                "arguments": deepcopy(arguments),
            }
        )
    return "\n\n".join(visible), "\n\n".join(reasoning), calls


def _usage_number(usage: Mapping[str, Any], name: str) -> int:
    value = usage.get(name)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _metric(usage: Any, stop_reason: Any) -> dict[str, Any]:
    if not isinstance(usage, Mapping):
        usage = {}
    prompt_tokens = (
        _usage_number(usage, "input_tokens")
        + _usage_number(usage, "cache_read_input_tokens")
        + _usage_number(usage, "cache_creation_input_tokens")
    )
    return {
        "prompt_tokens_len": prompt_tokens,
        "completion_tokens_len": _usage_number(usage, "output_tokens"),
        "llm_infer_ms": None,
        "env_action_ms": None,
        "stop_reason": stop_reason if isinstance(stop_reason, str) else "",
        "cache_read_tokens": _usage_number(usage, "cache_read_input_tokens"),
        "cache_write_tokens": _usage_number(usage, "cache_creation_input_tokens"),
    }


def _tool_result_content(block: Mapping[str, Any]) -> list[Any]:
    """Keep non-text blocks visible so text-only SFT validation cannot lose them."""

    content = block.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        text = "" if content is None else _stringify(content)
        return [{"type": "text", "text": text}]
    blocks: list[Any] = []
    for item in content:
        if isinstance(item, str):
            blocks.append({"type": "text", "text": item})
        elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
            blocks.append({"type": "text", "text": item["text"]})
        else:
            blocks.append(deepcopy(item))
    return blocks


def _normalize_tool(tool: Any) -> dict[str, Any] | None:
    if not isinstance(tool, Mapping):
        return None
    if tool.get("type") == "function" and isinstance(tool.get("function"), Mapping):
        function = deepcopy(tool["function"])
    else:
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            return None
        parameters = tool.get(
            "input_schema", tool.get("inputSchema", tool.get("parameters", {}))
        )
        if not isinstance(parameters, Mapping):
            parameters = {"type": "object", "additionalProperties": True}
        function = {
            "name": name,
            "description": (
                tool.get("description")
                if isinstance(tool.get("description"), str)
                else "Tool exposed by the Claude Code harness."
            ),
            "parameters": deepcopy(parameters),
        }
    name = function.get("name")
    if not isinstance(name, str) or not name:
        return None
    return {"type": "function", "function": function}


def _json_type(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        return {"type": "array"}
    if isinstance(value, dict):
        return {"type": "object"}
    return {}


def _inferred_tools(steps: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    fields_by_name: dict[str, dict[str, dict[str, Any]]] = {}
    for step in steps:
        assistant = step.get("assistant_content")
        calls = assistant.get("tool_calls") if isinstance(assistant, Mapping) else None
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, Mapping):
                continue
            name = call.get("name")
            arguments = call.get("arguments")
            if not isinstance(name, str) or not name:
                continue
            fields = fields_by_name.setdefault(name, {})
            if isinstance(arguments, Mapping):
                for key, value in arguments.items():
                    if isinstance(key, str):
                        fields.setdefault(key, _json_type(value))
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": "Tool invoked by the Claude Code native harness.",
                "parameters": {
                    "type": "object",
                    "properties": fields,
                    "additionalProperties": True,
                },
            },
        }
        for name, fields in sorted(fields_by_name.items())
    ]


def extract_anthropic_wire_context(
    path: Path,
) -> tuple[str, list[dict[str, Any]] | None]:
    """Read the first Anthropic system prompt and tools schema from a wire trace."""

    system_prompt = ""
    tools: list[dict[str, Any]] | None = None
    if not path.is_file():
        return system_prompt, tools
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, Mapping):
                continue
            body = entry.get("body")
            params = entry.get("params")
            container = body if isinstance(body, Mapping) else params
            if not system_prompt and isinstance(container, Mapping):
                system_prompt = _content_text(container.get("system"))
            raw_tools = entry.get("tools")
            if not isinstance(raw_tools, list) and isinstance(container, Mapping):
                raw_tools = container.get("tools")
            if tools is None and isinstance(raw_tools, list):
                normalized = [
                    item
                    for raw_tool in raw_tools
                    if (item := _normalize_tool(raw_tool)) is not None
                ]
                if normalized:
                    tools = normalized
            if system_prompt and tools is not None:
                break
    return system_prompt, tools


def _matches_expected_user(raw: str, expected: str) -> bool:
    return raw == expected or raw.strip() == expected.strip()


def claude_code_events_to_actf(
    events: Iterable[dict[str, Any]],
    *,
    user_messages: Iterable[str] | None = None,
    system_prompt: str = "",
    tools: list[dict[str, Any]] | None = None,
    keep_thinking: bool = True,
    notification_inputs: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Convert Claude Code user/assistant session events to ``ACTF_v1.0``."""


    raw_rows = list(events)
    seen_uuids: set[str] = set()
    rows: list[dict[str, Any]] = []
    for row in raw_rows:
        event_uuid = row.get("uuid")
        if isinstance(event_uuid, str) and event_uuid:
            if event_uuid in seen_uuids:
                continue
            seen_uuids.add(event_uuid)
        rows.append(row)
    rows.sort(key=lambda row: str(row.get("timestamp", "")))

    expected_users = tuple(user_messages) if user_messages is not None else None
    expected_index = 0
    pending_user = ""
    pending_user_time: str | None = None
    turns: list[dict[str, Any]] = []
    turns_by_message_id: dict[str, dict[str, Any]] = {}
    pending_calls: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    completed_calls: set[str] = set()

    last_usage_by_id: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if row.get("type") != "assistant":
            continue
        message = row.get("message")
        if not isinstance(message, Mapping):
            continue
        message_id = message.get("id")
        usage = message.get("usage")
        if isinstance(message_id, str) and isinstance(usage, Mapping):
            last_usage_by_id[message_id] = usage

    for event_index, row in enumerate(rows):
        message = row.get("message")
        if not isinstance(message, Mapping):
            continue
        event_type = row.get("type")
        timestamp = _timestamp(row.get("timestamp"))
        sidechain = bool(row.get("isSidechain", False))
        # A native child conversation is not part of the parent's model input.
        # Keep it in raw/capture artifacts, never flatten it into root steps.
        if sidechain or message.get("model") == "<synthetic>":
            continue

        if event_type == "assistant":
            visible, reasoning, calls = _assistant_parts(message.get("content"))
            message_id = message.get("id")
            key = (
                message_id
                if isinstance(message_id, str) and message_id
                else f"event-{event_index}"
            )
            turn = turns_by_message_id.get(key)
            if turn is None:
                usage = (
                    last_usage_by_id.get(message_id, message.get("usage"))
                    if isinstance(message_id, str)
                    else message.get("usage")
                )
                turn = {
                    "step_id": 0,
                    "assistant_content": {
                        "content": "",
                        "reasoning_content": "",
                        "tool_calls": [],
                    },
                    "metric": _metric(usage, message.get("stop_reason")),
                    "user_content": "" if sidechain else pending_user,
                    "tools": [],
                    "observation": [],
                    "started_at": timestamp or pending_user_time or "",
                    "finished_at": timestamp or pending_user_time or "",
                    "extra": {
                        "is_sidechain": sidechain,
                        "model": message.get("model"),
                        "message_id": message_id,
                    },
                }
                if not sidechain:
                    pending_user = ""
                    pending_user_time = None
                turns.append(turn)
                turns_by_message_id[key] = turn
            assistant = turn["assistant_content"]
            if visible:
                assistant["content"] = (
                    f"{assistant['content']}\n\n{visible}"
                    if assistant["content"]
                    else visible
                )
            if keep_thinking and reasoning:
                assistant["reasoning_content"] = (
                    f"{assistant['reasoning_content']}\n\n{reasoning}"
                    if assistant["reasoning_content"]
                    else reasoning
                )
            existing_ids = {
                call.get("id")
                for call in assistant["tool_calls"]
                if isinstance(call, Mapping)
            }
            for call in calls:
                call_id = call["id"]
                if call_id in existing_ids or call_id in completed_calls:
                    continue
                assistant["tool_calls"].append(call)
                turn["tools"].append(deepcopy(call))
                pending_calls[call_id] = (turn, call)
                existing_ids.add(call_id)
            turn["finished_at"] = timestamp or turn["finished_at"]
            stop_reason = message.get("stop_reason")
            if isinstance(stop_reason, str):
                turn["metric"]["stop_reason"] = stop_reason
            continue

        if event_type != "user":
            continue
        content = message.get("content")
        tool_results: list[Mapping[str, Any]] = []
        text_parts: list[str] = []
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, Mapping) and block.get("type") == "tool_result":
                    tool_results.append(block)
                elif isinstance(block, Mapping) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
                elif isinstance(block, str):
                    text_parts.append(block)

        for block in tool_results:
            call_id = block.get("tool_use_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            pending = pending_calls.pop(call_id, None)
            if pending is None or call_id in completed_calls:
                continue
            turn, call = pending
            details: dict[str, Any] = {"raw_tool_result": deepcopy(block)}
            tool_use_result = row.get("toolUseResult")
            if isinstance(tool_use_result, Mapping):
                details["toolUseResult"] = deepcopy(tool_use_result)
            turn["observation"].append(
                {
                    "role": "toolResult",
                    "toolCallId": call_id,
                    "toolName": call["name"],
                    "content": _tool_result_content(block),
                    "isError": bool(block.get("is_error", False)),
                    "timestamp": timestamp,
                    "details": details,
                }
            )
            turn["finished_at"] = timestamp or turn["finished_at"]
            completed_calls.add(call_id)

        if sidechain or tool_results:
            continue
        raw_text = "\n\n".join(part for part in text_parts if part)
        if not raw_text.strip():
            continue
        selected: str | None = None
        if isinstance(row.get("origin"), Mapping) and row["origin"].get("kind") == "task-notification":
            # Native completion is input to the root model, not child output
            # to flatten or another user-supplied prompt to filter out.
            selected = (notification_inputs or {}).get(raw_text, raw_text)
        elif expected_users is None:
            selected = raw_text
        elif expected_index < len(expected_users):
            for index in range(expected_index, len(expected_users)):
                expected = expected_users[index]
                if _matches_expected_user(raw_text, expected):
                    selected = expected
                    expected_index = index + 1
                    break
        if selected is not None:
            pending_user = (
                f"{pending_user}\n\n{selected}" if pending_user else selected
            )
            pending_user_time = pending_user_time or timestamp


    steps: list[dict[str, Any]] = []
    for turn in turns:
        assistant = turn["assistant_content"]
        if not (
            assistant["content"]
            or assistant["reasoning_content"]
            or assistant["tool_calls"]
        ):
            continue
        turn["step_id"] = len(steps) + 1
        steps.append(turn)

    normalized_tools = None
    if tools is not None:
        normalized_tools = [
            item for tool in tools if (item := _normalize_tool(tool)) is not None
        ]
    if normalized_tools is None:
        normalized_tools = _inferred_tools(steps)

    first_time = next(
        (_timestamp(row.get("timestamp")) for row in rows if _timestamp(row.get("timestamp"))),
        "",
    )
    last_time = next(
        (
            _timestamp(row.get("timestamp"))
            for row in reversed(rows)
            if _timestamp(row.get("timestamp"))
        ),
        first_time,
    )
    result = {
        "schema_version": "ACTF_v1.0",
        "system_prompt": system_prompt,
        "tools": normalized_tools,
        "steps": steps,
        "started_at": steps[0]["started_at"] if steps else first_time,
        "finished_at": steps[-1]["finished_at"] if steps else last_time,
    }
    if any(row.get("subtype") == "compact_boundary" for row in rows):
        result["sft_unavailable_reason"] = CLAUDE_COMPACTION_SFT_REASON
    return result


def convert_claude_code_trace(
    source: Path,
    destination: Path,
    *,
    user_messages: Iterable[str] | None = None,
    wire_trace: Path | None = None,
    keep_thinking: bool = True,
) -> dict[str, Any]:
    system_prompt = ""
    tools = None
    notification_inputs: dict[str, str] = {}
    if wire_trace is not None:
        system_prompt, tools = extract_anthropic_wire_context(wire_trace)
        if wire_trace.is_file():
            for entry in _read_events(wire_trace):
                body = entry.get("body", {})
                for message in body.get("messages", []) if isinstance(body, Mapping) else []:
                    if not isinstance(message, Mapping) or message.get("role") != "user":
                        continue
                    content = message.get("content", [])
                    if isinstance(content, str):
                        texts = [content]
                    elif isinstance(content, list):
                        texts = [b.get("text", "") for b in content if isinstance(b, Mapping)]
                    else:
                        texts = []
                    for text in texts:
                        if isinstance(text, str) and text.startswith("[SYSTEM NOTIFICATION - NOT USER INPUT]\n") and "<task-notification>" in text:
                            notification_inputs[text[text.index("<task-notification>"):]] = text
    trajectory = claude_code_events_to_actf(
        _read_events(source),
        user_messages=user_messages,
        system_prompt=system_prompt,
        tools=tools,
        keep_thinking=keep_thinking,
        notification_inputs=notification_inputs,
    )
    atomic_write_json(destination, trajectory)
    return trajectory
