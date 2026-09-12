"""Project OpenCode's native session export and request journal into ACTF."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable

from roller.actf import openclaw_events_to_actf
from roller.storage import atomic_write_json
from roller.usage import ZERO_USAGE


def read_opencode_events(path: Path) -> list[dict[str, Any]]:
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not all(isinstance(e, dict) for e in events):
        raise ValueError("OpenCode journal must contain JSON object events")
    return events


def native_messages(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages, parts = {}, {}
    for event in events:
        if event.get("type") == "session_export":
            for message in event["data"]["messages"]:
                info = message["info"]
                messages[info["id"]] = deepcopy(info)
                for part in message["parts"]:
                    parts[part["id"]] = deepcopy(part)
        elif event.get("type") == "event":
            native = event["event"]
            if native.get("type") == "message.updated":
                info = native["properties"]["info"]
                messages[info["id"]] = deepcopy(info)
            elif native.get("type") == "message.part.updated":
                part = native["properties"]["part"]
                parts[part["id"]] = deepcopy(part)
    # OpenCode's native IDs are sortable and preserve creation order. Use time
    # first for messages; updates must not move a message to the end.
    return [{"info": info, "parts": sorted((p for p in parts.values() if p["messageID"] == info["id"]), key=lambda p: p["id"])}
            for info in sorted(messages.values(), key=lambda i: (i.get("time", {}).get("created", 0), i["id"]))]


def _timestamp(milliseconds):
    if isinstance(milliseconds, (int, float)):
        return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat()
    return None


def opencode_events_to_actf(events: Iterable[dict[str, Any]], *, keep_thinking: bool = True) -> dict[str, Any]:
    events = list(events)
    schemas, systems = {}, []
    for event in events:
        if event.get("type") != "request":
            continue
        body = event["body"]
        for tool in body.get("tools", []):
            name = tool.get("function", {}).get("name")
            if not name:
                raise ValueError("OpenCode tool schema has no function name")
            if name in schemas and schemas[name] != tool:
                raise ValueError(f"OpenCode tool schema changed: {name}")
            schemas[name] = deepcopy(tool)
        for message in body.get("messages", []):
            if message.get("role") == "system":
                text = message.get("content")
                if not isinstance(text, str):
                    raise ValueError("OpenCode system prompt must be text")
                if text not in systems:
                    systems.append(text)
    if not systems:
        raise ValueError("OpenCode journal has no captured system prompt")
    rows, calls_seen = [], set()
    for native in native_messages(events):
        info, parts = native["info"], native["parts"]
        role = info["role"]
        if role not in {"user", "assistant"}:
            raise ValueError(f"unsupported OpenCode message role: {role}")
        if info.get("error"):
            raise ValueError("OpenCode native assistant contains an error")
        content, results = [], []
        for part in parts:
            kind = part["type"]
            if kind == "text":
                if not isinstance(part.get("text"), str):
                    raise ValueError("OpenCode text part must contain text")
                content.append({"type": "text", "text": part["text"]})
            elif kind == "reasoning" and role == "assistant":
                content.append({"type": "thinking", "thinking": part["text"]})
            elif kind == "tool" and role == "assistant":
                state = part["state"]
                if state["status"] not in {"completed", "error"}:
                    raise ValueError("OpenCode tool did not complete")
                if state.get("attachments"):
                    raise ValueError("OpenCode tool attachments cannot be exported as text-only SFT")
                call_id, name, arguments = part["callID"], part["tool"], state["input"]
                if not isinstance(arguments, dict):
                    raise ValueError("OpenCode tool arguments must be a JSON object")
                if not call_id or call_id in calls_seen:
                    raise ValueError("OpenCode tool call ID is missing or repeated")
                if name not in schemas:
                    raise ValueError(f"OpenCode tool call has no captured schema: {name}")
                calls_seen.add(call_id)
                content.append({"type": "toolCall", "id": call_id, "name": name, "arguments": arguments})
                result = state.get("error") if state["status"] == "error" else state.get("output")
                if not isinstance(result, str):
                    raise ValueError("OpenCode tool result must be text")
                results.append({"type": "message", "timestamp": _timestamp(state.get("time", {}).get("end")), "message": {"role": "toolResult", "toolCallId": call_id, "toolName": name, "content": result, "isError": state["status"] == "error"}})
            elif kind in {"step-start", "step-finish", "snapshot", "patch", "retry"}:
                continue  # Execution metadata remains in the native journal.
            else:
                raise ValueError(f"OpenCode part would lose data in text-only SFT: {kind}")
        tokens = info.get("tokens") or {}
        cache = tokens.get("cache") or {}
        message = {"role": role, "content": content, "usage": {"input": (tokens.get("input") or 0) + (cache.get("read") or 0) + (cache.get("write") or 0), "output": tokens.get("output") or 0}, "stopReason": info.get("finish", "")}
        rows.append({"type": "message", "timestamp": _timestamp(info.get("time", {}).get("created")), "message": message})
        rows.extend(results)
    return openclaw_events_to_actf(rows, system_prompts=["\n\n".join(systems)], tools=list(schemas.values()), keep_thinking=keep_thinking)


def convert_opencode_trace(source: Path, destination: Path, *, keep_thinking: bool = True) -> dict[str, Any]:
    trajectory = opencode_events_to_actf(read_opencode_events(source), keep_thinking=keep_thinking)
    atomic_write_json(destination, trajectory)
    return trajectory


def extract_opencode_usage(source: Path) -> dict[str, int | float]:
    totals = dict(ZERO_USAGE)
    for message in native_messages(read_opencode_events(source)):
        info = message["info"]
        if info.get("role") != "assistant":
            continue
        tokens = info.get("tokens") or {}
        cache = tokens.get("cache") or {}
        values = {"input_tokens": tokens.get("input", 0), "output_tokens": tokens.get("output", 0), "cache_read_tokens": cache.get("read", 0), "cache_write_tokens": cache.get("write", 0), "cost_usd": info.get("cost", 0)}
        totals["request_count"] += 1
        for key, value in values.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] += value
        total = tokens.get("total")
        totals["total_tokens"] += total if isinstance(total, (int, float)) else sum(values[k] or 0 for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
    return totals
