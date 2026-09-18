"""Convert pinned DeepSeek SDK events with captured native request schemas."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable

from roller.actf import openclaw_events_to_actf
from roller.storage import atomic_write_json
from roller.usage import ZERO_USAGE


def read_deepseek_events(path: Path) -> list[dict[str, Any]]:
    # JSONL uses LF as its record separator.  ``splitlines`` would also split
    # legal U+2028/U+2029 characters embedded in JSON strings.
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]
    if not all(isinstance(event, dict) for event in events):
        raise ValueError("DeepSeek journal must contain JSON objects")
    return events


def native_events(events):
    for event in events:
        if event.get("type") == "notification" and event.get("method") == "session.event":
            yield event["payload"]["event"]


def _text(blocks):
    if not isinstance(blocks, list) or any(b.get("type") != "text" or not isinstance(b.get("text"), str) for b in blocks):
        raise ValueError("DeepSeek content cannot be represented as text-only SFT")
    return "".join(block["text"] for block in blocks)


def deepseek_events_to_actf(events: Iterable[dict[str, Any]], *, keep_thinking: bool = True) -> dict[str, Any]:
    events = list(events)
    schemas, systems = {}, []
    for event in events:
        if event.get("type") == "runner_error":
            raise ValueError("DeepSeek journal contains a runner error")
        if event.get("type") != "request":
            continue
        for tool in event["body"].get("tools", []):
            name = tool.get("function", {}).get("name")
            if not name or (name in schemas and schemas[name] != tool):
                raise ValueError("DeepSeek tool schema is missing or changed")
            schemas[name] = deepcopy(tool)
        for message in event["body"].get("messages", []):
            if message.get("role") == "system":
                content = message["content"]
                if not isinstance(content, str):
                    raise ValueError("DeepSeek system prompt must be text")
                if content not in systems:
                    systems.append(content)
    if not systems:
        raise ValueError("DeepSeek journal has no captured system prompt")
    rows, calls, results, turns = [], {}, set(), []
    active_turn = None
    for event in native_events(events):
        kind, data = event["type"], event["data"]
        if kind == "turn/start":
            if active_turn is not None:
                raise ValueError("DeepSeek turn boundaries overlap")
            active_turn = data["turn"]
            continue
        if kind == "turn/end":
            if active_turn is None or data["turn"] != active_turn:
                raise ValueError("DeepSeek turn end has no matching start")
            active_turn = None
            turns.append(data["reason"]["kind"])
            continue
        if kind not in {"user/message", "assistant/message", "tool/result"}:
            continue
        timestamp = datetime.fromtimestamp(event["time"] / 1000, timezone.utc).isoformat()
        if event.get("surfaceOp") != "append":
            raise ValueError("DeepSeek surface replacement is unsupported by this profile")
        message = data if kind == "user/message" else data["message"]
        if kind == "tool/result":
            for block in message["content"]:
                call_id = block.get("toolCallId")
                if block.get("type") != "tool-result" or call_id not in calls or call_id in results:
                    raise ValueError("DeepSeek tool result has no unique matching call")
                results.add(call_id)
                rows.append({"type": "message", "timestamp": timestamp, "message": {
                    "role": "toolResult", "toolCallId": call_id, "toolName": calls[call_id],
                    "content": _text(block["content"]), "isError": block.get("isError", False)}})
            continue
        content = []
        for block in message["content"]:
            block_type = block["type"]
            if block_type == "text" and isinstance(block.get("text"), str):
                content.append(deepcopy(block))
            elif block_type == "reasoning" and kind == "assistant/message" and isinstance(block.get("text"), str):
                content.append({"type": "thinking", "thinking": block["text"]})
            elif block_type == "tool-call" and kind == "assistant/message":
                call_id, name = block["id"], block["name"]
                arguments = json.loads(block["arguments"])
                if not isinstance(arguments, dict) or not call_id or call_id in calls or name not in schemas:
                    raise ValueError("DeepSeek tool call is invalid or has no captured schema")
                calls[call_id] = name
                content.append({"type": "toolCall", "id": call_id, "name": name, "arguments": arguments})
            else:
                raise ValueError(f"DeepSeek content would lose data in text-only SFT: {block_type}")
        if data.get("interrupted"):
            raise ValueError("DeepSeek assistant stream was interrupted")
        tokens = data.get("usage") or {}
        rows.append({"type": "message", "timestamp": timestamp, "message": {
            "role": "user" if kind == "user/message" else "assistant", "content": content,
            "usage": {"input": sum(tokens.get(k, 0) for k in ("inputTokens", "cacheReadTokens", "cacheWriteTokens")), "output": tokens.get("outputTokens", 0)},
            "stopReason": "tool_calls" if any(b["type"] == "toolCall" for b in content) else "stop"}})
    if active_turn is not None or not turns or any(reason != "completed" for reason in turns) or set(calls) != results:
        raise ValueError("DeepSeek journal contains incomplete turns or tool calls")
    return openclaw_events_to_actf(rows, system_prompts=["\n\n".join(systems)], tools=list(schemas.values()), keep_thinking=keep_thinking)


def convert_deepseek_trace(source: Path, destination: Path, *, keep_thinking: bool = True) -> dict[str, Any]:
    trajectory = deepseek_events_to_actf(read_deepseek_events(source), keep_thinking=keep_thinking)
    atomic_write_json(destination, trajectory)
    return trajectory


def extract_deepseek_usage(source: Path) -> dict[str, int | float]:
    events = read_deepseek_events(source)
    totals = dict(ZERO_USAGE)
    totals["request_count"] = sum(e.get("type") == "request" for e in events)
    for event in native_events(events):
        if event["type"] != "assistant/message":
            continue
        tokens = event["data"].get("usage") or {}
        for output, native in (("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"),
                               ("cache_read_tokens", "cacheReadTokens"), ("cache_write_tokens", "cacheWriteTokens")):
            totals[output] += tokens.get(native, 0)
        totals["total_tokens"] += tokens.get("totalTokens", sum(tokens.get(k, 0) for k in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")))
    return totals
