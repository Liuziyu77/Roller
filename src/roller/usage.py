from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from roller.openclaw_delivery import OpenClawDeliveryTracker


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
    delivery_tracker = OpenClawDeliveryTracker()
    if not trace_path.is_file():
        return totals
    with trace_path.open("r", encoding="utf-8", errors="replace") as stream:
        for raw_line in stream:
            try:
                event = json.loads(raw_line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            delivery = delivery_tracker.observe(event)
            if event.get("type") != "message":
                continue
            message = event.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            if delivery.skip_usage:
                # CLI-only replies are counted; verified mirrors and already
                # recorded turn totals are not additional model requests.
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
                # OpenClaw's totalTokens includes the cache fields; every
                # observed trace satisfies total = input+output+cacheRead+
                # cacheWrite. Keep the fallback aligned with that semantic
                # (and with _counts in openclaw_delivery) so a provider event
                # missing the total is not undercounted next to a CLI mirror
                # carrying an explicit one.
                total_tokens = (
                    _number(usage.get("input"))
                    + _number(usage.get("output"))
                    + _number(usage.get("cacheRead"))
                    + _number(usage.get("cacheWrite"))
                )
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


def extract_claude_code_usage(
    trace_path: Path,
    stream_path: Path | None = None,
) -> dict[str, int | float]:
    """Sum final per-message usage from a Claude Code native session trace."""

    totals = dict(ZERO_USAGE)
    if not trace_path.is_file():
        return totals

    usage_by_message: dict[str, dict[str, Any]] = {}
    anonymous_usage: list[dict[str, Any]] = []
    with trace_path.open("r", encoding="utf-8", errors="replace") as stream:
        for raw_line in stream:
            try:
                event = json.loads(raw_line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(event, dict) or event.get("type") != "assistant":
                continue
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            message_id = message.get("id")
            if isinstance(message_id, str) and message_id:
                usage_by_message[message_id] = usage
            else:
                anonymous_usage.append(usage)

    usages = [*usage_by_message.values(), *anonymous_usage]
    totals["request_count"] = len(usages)
    for usage in usages:
        input_tokens = _number(usage.get("input_tokens"))
        output_tokens = _number(usage.get("output_tokens"))
        cache_read = _number(usage.get("cache_read_input_tokens"))
        cache_write = _number(usage.get("cache_creation_input_tokens"))
        totals["input_tokens"] += input_tokens
        totals["output_tokens"] += output_tokens
        totals["cache_read_tokens"] += cache_read
        totals["cache_write_tokens"] += cache_write
        totals["total_tokens"] += (
            input_tokens + output_tokens + cache_read + cache_write
        )

    if stream_path is not None and stream_path.is_file():
        total_cost = 0.0
        with stream_path.open("r", encoding="utf-8", errors="replace") as stream:
            for raw_line in stream:
                try:
                    event = json.loads(raw_line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(event, dict) or event.get("type") != "result":
                    continue
                cost = event.get("total_cost_usd")
                if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                    total_cost += float(cost)
        totals["cost_usd"] = total_cost
    totals["cost_usd"] = round(float(totals["cost_usd"]), 6)
    return totals
