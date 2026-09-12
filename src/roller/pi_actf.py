"""Strict text ACTF projection of pinned Pi events and native API requests."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Iterable

from roller.actf import openclaw_events_to_actf
from roller.storage import atomic_write_json
from roller.usage import ZERO_USAGE


def read_pi_events(path: Path) -> list[dict[str, Any]]:
    # JSONL framing is LF only: Unicode separators are valid inside strings.
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]
    if not all(isinstance(event, dict) for event in events):
        raise ValueError("Pi journal must contain JSON objects")
    return events


def pi_events_to_actf(events: Iterable[dict[str, Any]], *, keep_thinking: bool = True) -> dict[str, Any]:
    events = list(events)
    rows, tools, system = [], None, None
    calls, completed_calls = {}, set()
    turn, turn_count, request_count, assistant_count = None, 0, 0, 0
    awaiting_reply = False
    last_role = None
    users = 0
    for record in events:
        kind = record.get("type")
        if kind == "runner_error":
            raise ValueError("Pi journal contains a runner error")
        if kind == "turn_start":
            if turn is not None or record.get("turn") != turn_count + 1:
                raise ValueError("Pi turn boundaries overlap or are out of order")
            turn = record["turn"]
            users, last_role = 0, None
            continue
        if kind == "turn_end":
            if turn is None or record.get("turn") != turn or users != 1 or last_role != "assistant" or awaiting_reply or set(calls) != completed_calls:
                raise ValueError("Pi turn is incomplete")
            turn = None
            turn_count += 1
            continue
        if kind not in {"request", "event", "context"}:
            continue
        if turn is None or record.get("turn") != turn:
            raise ValueError("Pi event is outside its turn")
        if kind == "context":
            continue
        if kind == "request":
            if awaiting_reply or users != 1 or set(calls) != completed_calls:
                raise ValueError("Pi request has incomplete prior messages")
            body = record["body"]
            schemas = body.get("tools")
            if not isinstance(schemas, list) or not schemas:
                raise ValueError("Pi request has no captured tool schemas")
            names = [tool.get("function", {}).get("name") for tool in schemas]
            if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
                raise ValueError("Pi tool schemas contain invalid or duplicate names")
            prompts = [message.get("content") for message in body.get("messages", []) if message.get("role") == "system"]
            if len(prompts) != 1 or not isinstance(prompts[0], str) or not prompts[0]:
                raise ValueError("Pi request must contain one captured system prompt")
            if tools is not None and (tools != schemas or system != prompts[0]):
                raise ValueError("Pi system prompt or tool schemas changed within the session")
            tools, system = deepcopy(schemas), prompts[0]
            request_count += 1
            awaiting_reply = True
            continue
        event = record["event"]
        if event.get("type") in {"compaction_start", "compaction_end", "auto_retry_start"}:
            raise ValueError("Pi compaction and retry cannot be represented as a linear trajectory")
        if event.get("type") != "message_end":
            continue
        message = deepcopy(event["message"])
        role = message.get("role")
        if role not in {"user", "assistant", "toolResult"}:
            raise ValueError(f"Pi message cannot be represented as text-only SFT: {role}")
        content = message.get("content")
        if isinstance(content, str) and role == "user":
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            raise ValueError("Pi message content must be text blocks")
        for block in content:
            if not isinstance(block, dict):
                raise ValueError("Pi content block must be an object")
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                continue
            if role == "assistant" and block_type == "thinking" and isinstance(block.get("thinking"), str):
                continue
            if role == "assistant" and block_type == "toolCall":
                call_id, name = block.get("id"), block.get("name")
                if not isinstance(call_id, str) or not call_id or call_id in calls or name not in {t["function"]["name"] for t in tools or []} or not isinstance(block.get("arguments"), dict):
                    raise ValueError("Pi tool call is invalid or lacks a captured schema")
                calls[call_id] = name
                continue
            raise ValueError(f"Pi content would lose data in text-only SFT: {block_type}")
        message["content"] = content
        if role == "user":
            if users or awaiting_reply or last_role is not None:
                raise ValueError("Pi turn has overlapping user messages")
            users += 1
        elif role == "assistant":
            reason = message.get("stopReason")
            has_calls = any(block["type"] == "toolCall" for block in content)
            if not awaiting_reply or reason not in {"stop", "toolUse"} or has_calls != (reason == "toolUse"):
                raise ValueError("Pi assistant response is incomplete or has no matching request")
            if not has_calls and not any(block["type"] == "text" and block["text"].strip() for block in content):
                raise ValueError("Pi assistant response is empty")
            awaiting_reply = False
            assistant_count += 1
            tokens = message.get("usage") or {}
            message["usage"] = {**tokens, "input": sum(tokens.get(key, 0) for key in ("input", "cacheRead", "cacheWrite"))}
        else:
            call_id = message.get("toolCallId")
            if awaiting_reply or call_id not in calls or call_id in completed_calls or message.get("toolName") != calls[call_id]:
                raise ValueError("Pi tool result has no unique matching call")
            completed_calls.add(call_id)
        last_role = role
        rows.append({"type": "message", "timestamp": message.get("timestamp"), "message": message})
    if turn is not None or not turn_count or not system or request_count != assistant_count or set(calls) != completed_calls:
        raise ValueError("Pi journal contains incomplete turns or requests")
    return openclaw_events_to_actf(rows, system_prompts=[system], tools=tools, keep_thinking=keep_thinking)


def convert_pi_trace(source: Path, destination: Path, *, keep_thinking: bool = True) -> dict[str, Any]:
    trajectory = pi_events_to_actf(read_pi_events(source), keep_thinking=keep_thinking)
    atomic_write_json(destination, trajectory)
    return trajectory


def extract_pi_usage(source: Path) -> dict[str, int | float]:
    totals = dict(ZERO_USAGE)
    for record in read_pi_events(source):
        if record.get("type") == "request":
            totals["request_count"] += 1
        event = record.get("event", {}) if record.get("type") == "event" else {}
        message = event.get("message", {})
        if event.get("type") != "message_end" or message.get("role") != "assistant":
            continue
        tokens = message.get("usage") or {}
        for output, native in (("input_tokens", "input"), ("output_tokens", "output"),
                               ("cache_read_tokens", "cacheRead"), ("cache_write_tokens", "cacheWrite")):
            totals[output] += tokens.get(native, 0)
        totals["total_tokens"] += tokens.get("totalTokens", sum(tokens.get(key, 0) for key in ("input", "output", "cacheRead", "cacheWrite")))
    return totals
