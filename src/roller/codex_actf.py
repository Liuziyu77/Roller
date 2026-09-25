"""Projection of Codex CLI native session JSONL into Roller ACTF."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from roller.actf import _merge_user_content, _user_content
from roller.storage import atomic_write_json


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


def _timestamp(value: Any) -> str | None:
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
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return "" if value is None else str(value)
    parts: list[str] = []
    for block in value:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _instruction_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        text = value.get("text")
        if isinstance(text, str):
            return text
    if isinstance(value, list):
        return "\n".join(
            part for item in value if (part := _instruction_text(item))
        )
    return ""


def _reasoning_text(payload: Mapping[str, Any]) -> str:
    content = payload.get("content")
    visible_parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                visible_parts.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                visible_parts.append(item["text"])
    if visible_parts:
        return "\n".join(part for part in visible_parts if part)

    summary = payload.get("summary")
    if not isinstance(summary, list):
        return ""
    parts: list[str] = []
    for item in summary:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(part for part in parts if part)


def _arguments(payload: Mapping[str, Any]) -> tuple[dict[str, Any], Any]:
    payload_type = payload.get("type")
    raw = payload.get("arguments") if payload_type == "function_call" else payload.get("input")
    if payload_type == "custom_tool_call" and isinstance(raw, str):
        return {"input": raw}, raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"input": raw}, raw
    else:
        parsed = raw
    if isinstance(parsed, dict):
        return deepcopy(parsed), raw
    if parsed is None:
        return {}, raw
    return {"value": deepcopy(parsed)}, raw


_OUTPUT_TEXT_BLOCK_TYPES = frozenset({"text", "input_text", "output_text"})
_OUTPUT_IMAGE_BLOCK_TYPES = frozenset({"image", "input_image"})
_OUTPUT_UNSUPPORTED_BLOCK_TYPES = frozenset({"input_file", "input_audio"})


def _output_block_kind(value: Any) -> str | None:
    """Classify a declared output block without validating its fields.

    Field validation belongs to the ACTF-to-SFT converter, which can report
    malformed or unsupported media instead of silently turning a recognizable
    typed block into JSON text.
    """

    if not isinstance(value, Mapping):
        return None
    block_type = value.get("type")
    if block_type in _OUTPUT_TEXT_BLOCK_TYPES:
        return "text"
    if block_type in _OUTPUT_IMAGE_BLOCK_TYPES:
        return "image"
    if block_type in _OUTPUT_UNSUPPORTED_BLOCK_TYPES:
        return "unsupported"
    return None


def _is_output_content_block_list(value: Any) -> bool:
    # An empty JSON array has no structural evidence that it is a typed block
    # list. The caller handles a native list object separately so a genuine
    # empty content-block list can still remain empty.
    if not isinstance(value, list) or not value:
        return False
    # A fully discriminated list is a content-block list even when one block
    # has malformed fields. An explicit image discriminator is also decisive:
    # keep the media semantics while losslessly normalizing ordinary siblings,
    # and let malformed known blocks fail validation instead of downgrading the
    # whole result to JSON text.
    block_kinds = [_output_block_kind(item) for item in value]
    return all(kind is not None for kind in block_kinds) or "image" in block_kinds


def _json_text_block(value: Any) -> dict[str, Any]:
    return {"type": "text", "text": json.dumps(value, ensure_ascii=False)}


def _normalize_output_block(value: Any) -> dict[str, Any]:
    """Normalize one member of a recognized content-block list."""

    if isinstance(value, str):
        return {"type": "text", "text": value}

    block_kind = _output_block_kind(value)
    if block_kind == "text":
        return {"type": "text", "text": deepcopy(value.get("text"))}
    if block_kind == "image":
        image = deepcopy(dict(value))
        image["type"] = "image"
        image_url = image.pop("image_url", None)
        if "data" not in image and image_url is not None:
            image["data"] = image_url
        return image
    if block_kind == "unsupported":
        # Multimodal SFT cannot represent these typed Responses blocks. Keep
        # them in ACTF so conversion fails explicitly instead of flattening
        # them into text.
        return deepcopy(dict(value))
    return _json_text_block(value)


def _output_content_blocks(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"type": "text", "text": value}]
    if isinstance(value, list):
        if not value:
            return []
        if not _is_output_content_block_list(value):
            return [_json_text_block(value)]
        # Classification recognizes block shapes; validation remains in the
        # ACTF-to-SFT converter so malformed known blocks fail explicitly.
        return [_normalize_output_block(item) for item in value]
    if value is None:
        return [{"type": "text", "text": ""}]
    if isinstance(value, Mapping):
        if _output_block_kind(value) is not None:
            return [_normalize_output_block(value)]
        return [_json_text_block(value)]
    return [_json_text_block(value)]


def _decode_output(value: Any) -> tuple[Any, bool]:
    """Decode JSON text while retaining whether the raw value was encoded."""

    if not isinstance(value, str):
        return value, False
    try:
        return json.loads(value), True
    except json.JSONDecodeError:
        return value, False


def _unwrap_output(value: Any) -> tuple[Any, dict[str, Any] | None]:
    """Extract the output/metadata envelope used by Codex tool results."""

    if not isinstance(value, Mapping):
        return value, None
    output = value.get("output") if "output" in value else value
    if output is None:
        output = json.dumps(value, ensure_ascii=False)
    metadata = value.get("metadata")
    return output, deepcopy(metadata) if isinstance(metadata, dict) else None


def _output_content(
    value: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    decoded, decoded_from_json_string = _decode_output(value)
    output, metadata = _unwrap_output(decoded)
    if (
        decoded_from_json_string
        and isinstance(output, list)
        and not _is_output_content_block_list(output)
    ):
        # Preserve an encoded ordinary JSON array as one JSON value. In
        # particular, the string "[]" must not collapse into no output.
        output = json.dumps(output, ensure_ascii=False)
    return _output_content_blocks(output), metadata


def _usage_number(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


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


def _tool_schemas(steps: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    properties: dict[str, dict[str, dict[str, Any]]] = {}
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
            fields = properties.setdefault(name, {})
            if isinstance(arguments, Mapping):
                for key, item in arguments.items():
                    if isinstance(key, str):
                        fields.setdefault(key, _json_type(item))
    schemas: list[dict[str, Any]] = []
    for name, fields in sorted(properties.items()):
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "Tool invoked by the Codex CLI native harness.",
                    "parameters": {
                        "type": "object",
                        "properties": fields,
                        "additionalProperties": True,
                    },
                },
            }
        )
    return schemas


def codex_events_to_actf(
    events: Iterable[dict[str, Any]],
    *,
    user_messages: Iterable[str] | None = None,
    keep_thinking: bool = True,
) -> dict[str, Any]:
    """Convert one Codex native rollout stream to ``ACTF_v1.0``.

    Codex emits reasoning, messages and tool calls as separate response items.
    A ``token_count`` event closes the corresponding model request, so all
    response items before that boundary are grouped into one ACTF assistant
    step. Tool outputs are paired by ``call_id`` even though they arrive after
    the model-request boundary.
    """

    rows = list(events)
    expected_users = tuple(user_messages or ())
    user_index = 0
    pending_user: str | list[Any] = ""
    pending_user_time: str | None = None
    pending_reasoning: list[str] = []
    system_parts: list[str] = []
    system_seen: set[str] = set()
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    calls_by_id: dict[str, dict[str, Any]] = {}
    first_time: str | None = None
    last_time: str | None = None

    def add_system(value: Any) -> None:
        text = _instruction_text(value).strip()
        if text and text not in system_seen:
            system_seen.add(text)
            system_parts.append(text)

    def group(timestamp: str | None = None) -> dict[str, Any]:
        nonlocal current, pending_user, pending_user_time, pending_reasoning
        if current is None:
            current = {
                "user_content": pending_user,
                "user_time": pending_user_time,
                "started_at": pending_user_time or timestamp or first_time or "",
                "finished_at": timestamp or pending_user_time or first_time or "",
                "content": [],
                "reasoning": pending_reasoning,
                "tool_calls": [],
                "metric": {},
            }
            pending_user = ""
            pending_user_time = None
            pending_reasoning = []
            groups.append(current)
        elif pending_reasoning:
            current["reasoning"].extend(pending_reasoning)
            pending_reasoning = []
        if timestamp:
            current["finished_at"] = timestamp
        return current

    def close_group(payload: Mapping[str, Any], timestamp: str | None) -> None:
        nonlocal current
        if current is None:
            return
        info = payload.get("info")
        last_usage = info.get("last_token_usage") if isinstance(info, Mapping) else None
        if isinstance(last_usage, Mapping):
            current["metric"] = {
                "prompt_tokens_len": _usage_number(last_usage.get("input_tokens")),
                "completion_tokens_len": _usage_number(last_usage.get("output_tokens")),
                "reasoning_tokens_len": _usage_number(
                    last_usage.get("reasoning_output_tokens")
                ),
                "cache_read_tokens_len": _usage_number(
                    last_usage.get("cached_input_tokens")
                ),
                "cache_write_tokens_len": _usage_number(
                    last_usage.get("cache_write_input_tokens")
                ),
            }
        if timestamp:
            current["finished_at"] = timestamp
        current = None

    for event in rows:
        timestamp = _timestamp(event.get("timestamp"))
        first_time = first_time or timestamp
        last_time = timestamp or last_time
        event_type = event.get("type")
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}

        if event_type == "session_meta":
            add_system(payload.get("base_instructions"))
            add_system(payload.get("instructions"))
            continue

        if event_type == "event_msg":
            payload_type = payload.get("type")
            if payload_type == "token_count":
                close_group(payload, timestamp)
            elif payload_type == "web_search_end":
                call_id = payload.get("call_id")
                if isinstance(call_id, str) and call_id:
                    action = payload.get("action")
                    arguments: dict[str, Any] = {}
                    if isinstance(payload.get("query"), str):
                        arguments["query"] = payload["query"]
                    if action is not None:
                        arguments["action"] = deepcopy(action)
                    call = {
                        "type": "toolCall",
                        "id": call_id,
                        "name": "web_search",
                        "arguments": arguments,
                        "output": json.dumps(
                            payload.get("results", []), ensure_ascii=False
                        ),
                        "output_time": timestamp,
                        "is_error": False,
                    }
                    group(timestamp)["tool_calls"].append(call)
                    calls_by_id[call_id] = call
            continue

        if event_type != "response_item":
            continue

        payload_type = payload.get("type")
        if payload_type == "reasoning":
            reasoning = _reasoning_text(payload)
            if reasoning:
                pending_reasoning.append(reasoning)
            continue

        if payload_type == "message":
            role = payload.get("role")
            content = _content_text(payload.get("content"))
            if role in {"developer", "system"}:
                add_system(content)
            elif role == "user":
                if user_index < len(expected_users):
                    expected = expected_users[user_index]
                    normalized_content = content.strip()
                    normalized_expected = expected.strip()
                    if normalized_content == normalized_expected or (
                        normalized_expected
                        and normalized_expected in normalized_content
                    ):
                        value = expected
                        user_index += 1
                    else:
                        if normalized_content.startswith("<environment_context>"):
                            add_system(content)
                        continue
                else:
                    value = content
                value = _user_content(payload.get("content"), value, native_text=content)
                if value:
                    pending_user = _merge_user_content(pending_user, value)
                    pending_user_time = pending_user_time or timestamp
            elif role == "assistant":
                target = group(timestamp)
                if content:
                    target["content"].append(content)
            continue

        if payload_type in {"function_call", "custom_tool_call"}:
            call_id = payload.get("call_id")
            name = payload.get("name")
            if not isinstance(call_id, str) or not call_id:
                continue
            if not isinstance(name, str) or not name:
                name = "unknown_tool"
            namespace = payload.get("namespace")
            if isinstance(namespace, str) and namespace and not name.startswith(namespace + "."):
                name = namespace + "." + name
            arguments, raw_arguments = _arguments(payload)
            call = {
                "type": "toolCall",
                "id": call_id,
                "name": name,
                "arguments": arguments,
                "raw_arguments": raw_arguments,
                "output": None,
                "output_time": None,
                "is_error": False,
            }
            group(timestamp)["tool_calls"].append(call)
            calls_by_id[call_id] = call
            continue

        if payload_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = payload.get("call_id")
            call = calls_by_id.get(call_id) if isinstance(call_id, str) else None
            if call is None:
                continue
            output, metadata = _output_content(payload.get("output"))
            call["output"] = output
            call["output_time"] = timestamp
            call["metadata"] = metadata
            exit_code = metadata.get("exit_code") if isinstance(metadata, Mapping) else None
            call["is_error"] = (
                isinstance(exit_code, int)
                and not isinstance(exit_code, bool)
                and exit_code != 0
            )

    if current is not None:
        current["finished_at"] = last_time or current["finished_at"]

    steps: list[dict[str, Any]] = []
    for raw_group in groups:
        calls: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        for raw_call in raw_group["tool_calls"]:
            calls.append(
                {
                    "type": "toolCall",
                    "id": raw_call["id"],
                    "name": raw_call["name"],
                    "arguments": deepcopy(raw_call["arguments"]),
                }
            )
            output = raw_call.get("output")
            missing = output is None
            if missing:
                content = [
                    {
                        "type": "text",
                        "text": "[Codex trace ended before this tool returned]",
                    }
                ]
            elif isinstance(output, list):
                content = deepcopy(output)
            else:
                content = _output_content_blocks(output)
            observations.append(
                {
                    "role": "toolResult",
                    "toolCallId": raw_call["id"],
                    "toolName": raw_call["name"],
                    "content": content,
                    "isError": bool(raw_call.get("is_error")) or missing,
                    **(
                        {"timestamp": raw_call["output_time"]}
                        if raw_call.get("output_time")
                        else {}
                    ),
                }
            )
        reasoning = "\n\n".join(
            item for item in raw_group["reasoning"] if isinstance(item, str) and item
        )
        metric = {
            "prompt_tokens_len": 0,
            "completion_tokens_len": 0,
            "llm_infer_ms": None,
            "env_action_ms": None,
            "stop_reason": "tool_use" if calls else "stop",
            **raw_group["metric"],
        }
        steps.append(
            {
                "step_id": len(steps) + 1,
                "assistant_content": {
                    "content": "\n\n".join(raw_group["content"]),
                    "reasoning_content": reasoning if keep_thinking else "",
                    "tool_calls": calls,
                },
                "metric": metric,
                "user_content": raw_group["user_content"],
                "tools": deepcopy(calls),
                "observation": observations,
                "started_at": raw_group["started_at"],
                "finished_at": raw_group["finished_at"],
            }
        )

    if steps and not any(step["user_content"] for step in steps) and expected_users:
        steps[0]["user_content"] = expected_users[0]

    return {
        "schema_version": "ACTF_v1.0",
        "system_prompt": "\n\n".join(system_parts),
        "tools": _tool_schemas(steps),
        "steps": steps,
        "started_at": steps[0]["started_at"] if steps else first_time or "",
        "finished_at": steps[-1]["finished_at"] if steps else last_time or first_time or "",
    }


def convert_codex_trace(
    source: Path,
    destination: Path,
    *,
    user_messages: Iterable[str] | None = None,
    keep_thinking: bool = True,
) -> dict[str, Any]:
    trajectory = codex_events_to_actf(
        _read_events(source),
        user_messages=user_messages,
        keep_thinking=keep_thinking,
    )
    atomic_write_json(destination, trajectory)
    return trajectory
