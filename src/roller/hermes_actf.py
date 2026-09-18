"""Loss-aware conversion of the Hermes runner's native message event journal."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Iterable

from roller.actf import openclaw_events_to_actf
from roller.storage import atomic_write_json
from roller.usage import ZERO_USAGE


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
                raise ValueError("Hermes assistant/user content is non-text; ACTF conversion would lose data")
            parts.append(block["text"])
        return "\n".join(parts)
    raise ValueError("Hermes message content must be text or text blocks")


def hermes_events_to_actf(events: Iterable[dict[str, Any]], *, keep_thinking: bool = True) -> dict[str, Any]:
    """Retain executed messages, even when Hermes compresses its active history.

    Request snapshots supply the actual system prompt and tool definitions;
    only journaled message events become conversation steps. OpenClaw's step
    builder is reused after explicitly translating Hermes/OpenAI message fields.
    """
    rows, tools, systems, call_names = [], {}, [], {}
    for event in events:
        kind = event.get("type")
        if kind == "request":
            for tool in event.get("tools", []):
                name = tool.get("function", {}).get("name")
                if not name:
                    raise ValueError("Hermes tool schema is missing its function name")
                if name in tools and tools[name] != tool:
                    raise ValueError(f"Hermes tool schema changed during the trajectory: {name}")
                tools[name] = deepcopy(tool)
            for message in event.get("messages", []):
                if message.get("role") == "system":
                    text = _text(message.get("content"))
                    if text and text not in systems:
                        systems.append(text)
            continue
        if kind != "message":
            continue
        message = deepcopy(event["message"])
        role = message.get("role")
        if role == "system":
            text = _text(message.get("content"))
            if text and text not in systems:
                systems.append(text)
            continue
        if role == "assistant":
            content = []
            text = _text(message.get("content"))
            reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
            if not isinstance(reasoning, str):
                raise ValueError("Hermes reasoning must be text")
            if text:
                content.append({"type": "text", "text": text})
            if reasoning:
                content.append({"type": "thinking", "thinking": reasoning})
            for call in message.get("tool_calls") or []:
                function = call["function"]
                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("Hermes tool arguments must encode a JSON object")
                call_id, name = call.get("id"), function.get("name")
                if not isinstance(call_id, str) or not call_id or call_id in call_names:
                    raise ValueError("Hermes tool call ID is missing or repeated")
                if name not in tools:
                    raise ValueError(f"Hermes tool call has no recorded schema: {name}")
                call_names[call_id] = name
                content.append({"type": "toolCall", "id": call_id, "name": name, "arguments": arguments})
            usage = event.get("usage") or {}
            message = {"role": "assistant", "content": content, "usage": {"input": usage.get("prompt_tokens", 0), "output": usage.get("completion_tokens", 0)}, "stopReason": event.get("finish_reason", "")}
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in call_names:
                raise ValueError(f"Hermes tool result has no preceding call: {call_id}")
            # Preserve typed blocks; the shared SFT exporter rejects unsupported media.
            message = {"role": "toolResult", "toolCallId": call_id, "toolName": call_names[call_id], "content": message.get("content", ""), "isError": bool(message.get("is_error", False))}
        elif role == "user":
            message["content"] = _text(message.get("content"))
        else:
            raise ValueError(f"unsupported Hermes message role: {role}")
        rows.append({"type": "message", "timestamp": event.get("timestamp"), "message": message})
    return openclaw_events_to_actf(rows, system_prompts=["\n\n".join(systems)], tools=list(tools.values()), keep_thinking=keep_thinking)


def read_hermes_events(source: Path) -> list[dict[str, Any]]:
    # Do not silently turn a truncated/corrupt journal into a successful sample.
    # Split only on LF: U+2028/U+2029 are valid JSON string content rather than
    # JSON Lines record separators.
    events = [json.loads(line) for line in source.read_text(encoding="utf-8").split("\n") if line.strip()]
    if not all(isinstance(event, dict) for event in events):
        raise ValueError("Hermes journal must contain JSON object events")
    return events


def convert_hermes_trace(source: Path, destination: Path, *, keep_thinking: bool = True) -> dict[str, Any]:
    trajectory = hermes_events_to_actf(read_hermes_events(source), keep_thinking=keep_thinking)
    atomic_write_json(destination, trajectory)
    return trajectory


def extract_hermes_usage(source: Path) -> dict[str, int | float]:
    totals = dict(ZERO_USAGE)
    for event in read_hermes_events(source):
        if event.get("type") != "message" or event.get("message", {}).get("role") != "assistant":
            continue
        usage = event.get("usage") or {}
        totals["request_count"] += 1
        for output, field in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"), ("total_tokens", "total_tokens")):
            value = usage.get(field, (usage.get("prompt_tokens", 0) or 0) + (usage.get("completion_tokens", 0) or 0) if field == "total_tokens" else 0)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[output] += value
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        if isinstance(cached, (int, float)) and not isinstance(cached, bool):
            totals["cache_read_tokens"] += cached
    return totals
