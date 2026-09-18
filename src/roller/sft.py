from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from roller.storage import atomic_write_json


class SFTConversionError(ValueError):
    """Raised when an ACTF trajectory cannot form a valid SFT conversation."""


class SFTUnsupportedContentError(SFTConversionError):
    """Raised when valid ACTF content cannot be represented without data loss."""


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise SFTConversionError(f"{field} must be a string")
    return value


def _normalize_tools(value: Any) -> list[dict[str, Any]]:
    # ``None`` means the wire trace did not expose a tools schema. It is still
    # safe to export trajectories that never called a tool; calls are checked
    # against this empty list below.
    if value is None:
        return []
    if not isinstance(value, list):
        raise SFTConversionError("tools must be a list or null")

    tools: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, tool in enumerate(value):
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise SFTConversionError(f"tools[{index}] must be a function tool")
        function = tool.get("function")
        if function is None:
            # The Responses API flattens function fields onto each tool item;
            # Chat Completions nests them below ``function``. SFT always uses
            # the nested OpenAI-compatible representation.
            function = {key: deepcopy(item) for key, item in tool.items() if key != "type"}
        elif not isinstance(function, dict):
            raise SFTConversionError(f"tools[{index}].function must be an object")
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise SFTConversionError(f"tools[{index}].function.name must be non-empty")
        if name in names:
            raise SFTConversionError(f"duplicate tool name: {name}")
        names.add(name)
        tools.append({"type": "function", "function": deepcopy(function)})
    return tools


def _arguments_json(value: Any, field: str) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SFTConversionError(f"{field} must contain valid JSON") from exc
    else:
        parsed = value
    if not isinstance(parsed, dict):
        raise SFTConversionError(f"{field} must encode a JSON object")
    try:
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise SFTConversionError(f"{field} is not JSON-serializable") from exc


def _normalize_tool_call(call: Any, field: str) -> dict[str, Any]:
    if not isinstance(call, dict):
        raise SFTConversionError(f"{field} must be an object")
    call_id = call.get("id")
    if not isinstance(call_id, str) or not call_id:
        raise SFTConversionError(f"{field}.id must be non-empty")

    function = call.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        arguments = function.get("arguments", {})
    else:
        name = call.get("name")
        arguments = call.get("arguments", {})
    if not isinstance(name, str) or not name:
        raise SFTConversionError(f"{field} tool name must be non-empty")
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": _arguments_json(arguments, f"{field}.arguments"),
        },
    }


def _tool_result_content(value: Any, field: str) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise SFTConversionError(f"{field} must be a string or text-block list")

    parts: list[str] = []
    for index, block in enumerate(value):
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict) or block.get("type") != "text":
            raise SFTUnsupportedContentError(
                f"{field}[{index}] is non-text; text-only SFT export would lose data"
            )
        text = block.get("text")
        if not isinstance(text, str):
            raise SFTConversionError(f"{field}[{index}].text must be a string")
        parts.append(text)
    return "\n".join(parts)


def actf_to_sft(trajectory: Mapping[str, Any]) -> dict[str, Any]:
    """Convert one ACTF trajectory to a minimal messages/tools SFT record."""

    system_prompt = trajectory.get("system_prompt", "")
    if not isinstance(system_prompt, str):
        raise SFTConversionError("system_prompt must be a string")
    tools = _normalize_tools(trajectory.get("tools"))
    tool_names = {tool["function"]["name"] for tool in tools}

    steps = trajectory.get("steps")
    if not isinstance(steps, list):
        raise SFTConversionError("steps must be a list")

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    seen_call_ids: set[str] = set()
    user_count = 0
    assistant_count = 0

    for step_index, step in enumerate(steps):
        prefix = f"steps[{step_index}]"
        if not isinstance(step, dict):
            raise SFTConversionError(f"{prefix} must be an object")

        user_content = _string(step.get("user_content", ""), f"{prefix}.user_content")
        if user_content:
            messages.append({"role": "user", "content": user_content})
            user_count += 1

        assistant = step.get("assistant_content")
        if not isinstance(assistant, dict):
            raise SFTConversionError(f"{prefix}.assistant_content must be an object")
        reasoning = _string(
            assistant.get("reasoning_content", ""),
            f"{prefix}.assistant_content.reasoning_content",
        )
        content = _string(
            assistant.get("content", ""), f"{prefix}.assistant_content.content"
        )
        raw_calls = assistant.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            raise SFTConversionError(
                f"{prefix}.assistant_content.tool_calls must be a list"
            )
        calls = [
            _normalize_tool_call(call, f"{prefix}.assistant_content.tool_calls[{index}]")
            for index, call in enumerate(raw_calls)
        ]
        if not reasoning and not content and not calls:
            raise SFTConversionError(f"{prefix} contains an empty assistant message")

        expected_ids: set[str] = set()
        call_names: dict[str, str] = {}
        for call in calls:
            call_id = call["id"]
            name = call["function"]["name"]
            if call_id in seen_call_ids:
                raise SFTConversionError(f"duplicate tool call id: {call_id}")
            if name not in tool_names:
                raise SFTConversionError(f"tool call references missing schema: {name}")
            seen_call_ids.add(call_id)
            expected_ids.add(call_id)
            call_names[call_id] = name

        observations = step.get("observation", [])
        if not isinstance(observations, list):
            raise SFTConversionError(f"{prefix}.observation must be a list")
        observed_ids: set[str] = set()
        tool_messages: list[dict[str, Any]] = []
        for observation_index, observation in enumerate(observations):
            field = f"{prefix}.observation[{observation_index}]"
            if not isinstance(observation, dict):
                raise SFTConversionError(f"{field} must be an object")
            call_id = observation.get("toolCallId", observation.get("tool_call_id"))
            if not isinstance(call_id, str) or not call_id:
                raise SFTConversionError(f"{field}.toolCallId must be non-empty")
            if call_id in observed_ids:
                raise SFTConversionError(f"duplicate tool result for call id: {call_id}")
            if call_id not in expected_ids:
                raise SFTConversionError(f"tool result has no matching call: {call_id}")
            observed_name = observation.get("toolName")
            if (
                isinstance(observed_name, str)
                and observed_name
                and observed_name != call_names[call_id]
            ):
                raise SFTConversionError(
                    f"tool result name {observed_name} does not match call {call_names[call_id]}"
                )
            observed_ids.add(call_id)
            tool_message = (
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _tool_result_content(
                        observation.get("content", []), f"{field}.content"
                    ),
                }
            )
            tool_messages.append(tool_message)
        if observed_ids != expected_ids:
            missing = sorted(expected_ids - observed_ids)
            raise SFTConversionError(f"tool calls missing results: {', '.join(missing)}")

        messages.append(
            {
                "role": "assistant",
                "reasoning_content": reasoning,
                "content": content,
                "tool_calls": calls,
            }
        )
        messages.extend(tool_messages)
        assistant_count += 1

    if user_count == 0:
        raise SFTConversionError("trajectory contains no user message")
    if assistant_count == 0:
        raise SFTConversionError("trajectory contains no assistant message")
    return {"messages": messages, "tools": tools}


def write_sft(
    path: Path,
    trajectory: Mapping[str, Any],
) -> dict[str, Any]:
    record = actf_to_sft(trajectory)
    atomic_write_json(path, record)
    return record
