from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ZERO_USAGE: dict[str, int | float] = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "total_tokens": 0,
    "cost_usd": 0.0,
    "request_count": 0,
}


def _number(value: Any, default: int | float = 0) -> int | float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def extract_openclaw_usage(trace_path: Path) -> dict[str, int | float]:
    """Sum usage blocks from raw OpenClaw assistant message events."""

    totals = dict(ZERO_USAGE)
    if not trace_path.is_file():
        return totals
    with trace_path.open("r", encoding="utf-8", errors="replace") as stream:
        for raw_line in stream:
            try:
                event = json.loads(raw_line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(event, dict) or event.get("type") != "message":
                continue
            message = event.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            totals["request_count"] += 1
            totals["input_tokens"] += _number(usage.get("input"))
            totals["output_tokens"] += _number(usage.get("output"))
            totals["cache_read_tokens"] += _number(usage.get("cacheRead"))
            totals["cache_write_tokens"] += _number(usage.get("cacheWrite"))
            total_tokens = _number(usage.get("totalTokens"), -1)
            if total_tokens == -1:
                total_tokens = _number(usage.get("input")) + _number(usage.get("output"))
            totals["total_tokens"] += total_tokens
            cost = usage.get("cost")
            if isinstance(cost, dict):
                totals["cost_usd"] += _number(cost.get("total"), 0.0)
    totals["cost_usd"] = round(float(totals["cost_usd"]), 6)
    return totals


def extract_codex_usage(trace_path: Path) -> dict[str, int | float]:
    """Read cumulative token usage from a Codex native session trace."""

    totals = dict(ZERO_USAGE)
    latest: dict[str, Any] | None = None
    if not trace_path.is_file():
        return totals
    with trace_path.open("r", encoding="utf-8", errors="replace") as stream:
        for raw_line in stream:
            try:
                event = json.loads(raw_line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(event, dict) or event.get("type") != "event_msg":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            last_usage = info.get("last_token_usage")
            if isinstance(last_usage, dict) and last_usage:
                totals["request_count"] += 1
            cumulative = info.get("total_token_usage")
            if isinstance(cumulative, dict):
                latest = cumulative
    if latest is None:
        return totals
    totals["input_tokens"] = _number(latest.get("input_tokens"))
    totals["output_tokens"] = _number(latest.get("output_tokens"))
    totals["cache_read_tokens"] = _number(latest.get("cached_input_tokens"))
    totals["cache_write_tokens"] = _number(latest.get("cache_write_input_tokens"))
    total_tokens = _number(latest.get("total_tokens"), -1)
    if total_tokens == -1:
        total_tokens = totals["input_tokens"] + totals["output_tokens"]
    totals["total_tokens"] = total_tokens
    return totals
