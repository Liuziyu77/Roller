from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from roller.openclaw_delivery import OpenClawDeliveryTracker
from roller.openclaw_usage import OpenClawUsage


def _event_time(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
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


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return "" if value is None else str(value)


def _tool_call(block: dict[str, Any]) -> dict[str, Any]:
    arguments = block.get("arguments", {})
    if not isinstance(arguments, dict):
        arguments = {"value": arguments}
    return {
        "type": "toolCall",
        "id": str(block.get("id", "")),
        "name": str(block.get("name", "")),
        "arguments": deepcopy(arguments),
    }


def _user_content(value: Any, text: str, *, native_text: str | None = None) -> str | list[Any]:
    """Keep upstream prompt cleanup and retain native image positions.

    A single text block has an unambiguous replacement location. Multiple
    unchanged text blocks can stay as-is. Otherwise use the upstream-selected
    text, without adding a rejection or inventing a block-placement policy.
    """
    if not isinstance(value, list):
        return text
    if all(isinstance(item, str) or (isinstance(item, dict)
           and item.get("type") in {"text", "input_text", "output_text"}) for item in value):
        return text
    if (native_text if native_text is not None else _text(value)) != text:
        text_indexes = [index for index, item in enumerate(value)
                        if isinstance(item, str) or (isinstance(item, dict)
                            and item.get("type") in {"text", "input_text", "output_text"}
                            and isinstance(item.get("text"), str))]
        if len(text_indexes) == 1:
            result = deepcopy(value)
            index = text_indexes[0]
            if isinstance(result[index], str):
                result[index] = text
            else:
                result[index]["text"] = text
            return result
        return text
    return deepcopy(value)


def _merge_user_content(previous: str | list[Any], value: str | list[Any]) -> str | list[Any]:
    if not previous:
        return deepcopy(value)
    if not value:
        return previous
    if isinstance(previous, str) and isinstance(value, str):
        return f"{previous}\n\n{value}"
    left = [{"type": "text", "text": previous}] if isinstance(previous, str) else deepcopy(previous)
    right = [{"type": "text", "text": value}] if isinstance(value, str) else deepcopy(value)
    return [*left, *right]


def _assistant_parts(content: Any) -> tuple[str, str, list[dict[str, Any]]]:
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
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            visible.append(block["text"])
        elif block_type in {"thinking", "reasoning"}:
            value = block.get("thinking", block.get("text", block.get("reasoning", "")))
            if isinstance(value, str):
                reasoning.append(value)
        elif block_type in {"toolCall", "tool_call"}:
            calls.append(_tool_call(block))
    return "\n".join(visible), "\n".join(reasoning), calls


def _observation(
    message: dict[str, Any], tool_call_namespace: str | None = None
) -> dict[str, Any]:
    content = message.get("content", [])
    if not isinstance(content, list):
        content = [{"type": "text", "text": _text(content)}]
    row: dict[str, Any] = {
        "role": "toolResult",
        "toolCallId": _tool_call_id(message.get("toolCallId", ""), tool_call_namespace),
        "toolName": str(message.get("toolName", "")),
        "content": deepcopy(content),
        "isError": bool(message.get("isError", False)),
    }
    if message.get("timestamp") is not None:
        row["timestamp"] = message["timestamp"]
    if "details" in message:
        row["details"] = deepcopy(message["details"])
    return row


def _tool_call_id(value: Any, namespace: str | None) -> str:
    identifier = str(value)
    return f"{namespace}:{identifier}" if namespace else identifier


def _message_event(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("type") != "message":
        return None
    message = raw.get("message")
    return message if isinstance(message, dict) else None


def _read_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                events.append(row)
    return events


def _cache_trace_system_prompts(path: Path) -> list[str]:
    """Extract the assembled system prompt of every LLM call from an OpenClaw cache-trace file.

    Prefers per-call ``stream:context.system`` entries; falls back to the
    session-level ``session:loaded.system`` snapshot (openclaw emits the
    assembled prompt there once per session).
    """
    per_call: list[str] = []
    session_level: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            system = row.get("system")
            if not isinstance(system, str):
                continue
            stage = row.get("stage")
            if stage == "stream:context":
                per_call.append(system)
            elif stage == "session:loaded":
                session_level.append(system)
    return per_call if per_call else session_level


def openclaw_events_to_actf(
    events: Iterable[dict[str, Any]],
    user_messages: Iterable[str] | None = None,
    system_prompts: Iterable[str] | None = None,
    tools: list[Any] | None = None,
    keep_thinking: bool = True,
) -> dict[str, Any]:
    """Project raw OpenClaw session events into the ACTF_v1.0 step shape.

    The system prompt and tools schema are stored once at the top level (they
    are session-level constants for a rollout); steps never repeat them.
    ``keep_thinking=False`` blanks ``reasoning_content`` in every step.
    """

    rows = list(events)
    multiple_sessions = sum(row.get("type") == "session" for row in rows) > 1
    session_index = 0
    delivery_tracker = OpenClawDeliveryTracker()
    system_prompt_list = list(system_prompts) if system_prompts is not None else []
    system_prompt = system_prompt_list[0] if system_prompt_list else ""
    first_time = next(
        (_timestamp(row.get("timestamp")) for row in rows if _timestamp(row.get("timestamp"))),
        None,
    )
    pending_user: str | list[Any] = ""
    pending_user_time: str | None = None
    expected_users = tuple(user_messages) if user_messages is not None else ()
    user_index = 0
    retry_after_empty_error = False
    steps: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    last_message_was_dropped_empty_attempt = False

    def finish_current(fallback: str | None = None) -> None:
        nonlocal current
        if current is None:
            return
        if not current.get("finished_at"):
            current["finished_at"] = fallback or current["started_at"]
        steps.append(current)
        current = None

    for row in rows:
        if row.get("type") == "session":
            session_index += 1
        delivery = delivery_tracker.observe(row) if isinstance(row, dict) else None
        message = _message_event(row)
        if message is None:
            continue
        last_message_was_dropped_empty_attempt = False
        role = message.get("role")
        message_time = _timestamp(row.get("timestamp") or message.get("timestamp"))
        if role == "user":
            value = (
                expected_users[user_index]
                if user_index < len(expected_users)
                else _text(message.get("content"))
            )
            value = _user_content(message.get("content"), value)
            if retry_after_empty_error and pending_user and value == pending_user:
                # OpenClaw records its internal provider retry as the same user
                # message again. Keep that transport detail in the raw trace,
                # but do not turn it into duplicate training context.
                retry_after_empty_error = False
                continue
            retry_after_empty_error = False
            finish_current(message_time)
            user_index += 1
            pending_user = _merge_user_content(pending_user, value)
            pending_user_time = pending_user_time or message_time
            continue
        if role == "assistant":
            if delivery is not None and delivery.skip_message:
                # Same-content CLI delivery duplicate of the provider reply;
                # skipping it before finish_current keeps the real step's
                # finished_at derived from genuine conversation events only.
                continue
            content, reasoning, tool_calls = _assistant_parts(message.get("content"))
            stop_reason = message.get("stopReason", message.get("stop_reason", ""))
            tool_call_namespace = (
                f"session-{session_index}" if multiple_sessions else None
            )
            for tool_call in tool_calls:
                tool_call["id"] = _tool_call_id(
                    tool_call.get("id", ""), tool_call_namespace
                )
            if not content.strip() and not reasoning.strip() and not tool_calls:
                # OpenClaw can emit an empty assistant event while finalizing a
                # tool turn (and also before retrying a provider error). Keep the
                # event in the native trace for auditability, but do not turn it
                # into an invalid empty ACTF/SFT assistant message.
                retry_after_empty_error = bool(message.get("errorMessage"))
                last_message_was_dropped_empty_attempt = stop_reason in {
                    "aborted",
                    "error",
                    "length",
                }
                continue
            retry_after_empty_error = False
            finish_current(message_time)
            # Malformed counters are sanitized by OpenClawUsage so they can
            # neither crash (NaN/Infinity int() conversion) nor contaminate
            # step metrics with negative values. skip_usage stays the only
            # deduplication decision; a retained gap-fill reply keeps its
            # text without duplicating the recorded step token metrics.
            parsed = (
                None
                if delivery is not None and delivery.skip_usage
                else OpenClawUsage.parse(message.get("usage"))
            )
            started_at = (
                first_time or pending_user_time or message_time
                if not steps
                else pending_user_time or steps[-1]["finished_at"]
            )
            finished_at = None if tool_calls else message_time or started_at
            current = {
                "step_id": len(steps) + 1,
                "assistant_content": {
                    "content": content,
                    "reasoning_content": reasoning if keep_thinking else "",
                    "tool_calls": deepcopy(tool_calls),
                },
                "metric": {
                    "prompt_tokens_len": int(parsed.counts[0]) if parsed else 0,
                    "completion_tokens_len": int(parsed.counts[1]) if parsed else 0,
                    "llm_infer_ms": None,
                    "env_action_ms": None,
                    "stop_reason": stop_reason,
                },
                "user_content": pending_user,
                "tools": deepcopy(tool_calls),
                "observation": [],
                "started_at": started_at or "",
                "finished_at": finished_at,
            }
            pending_user = ""
            pending_user_time = None
            continue
        if role == "toolResult":
            if current is None:
                raise ValueError("orphan tool result: native assistant tool call is missing")
            tool_call_namespace = (
                f"session-{session_index}" if multiple_sessions else None
            )
            current["observation"].append(
                _observation(message, tool_call_namespace)
            )
            current["finished_at"] = message_time or current.get("finished_at")

    last_time = next(
        (_timestamp(row.get("timestamp")) for row in reversed(rows) if _timestamp(row.get("timestamp"))),
        None,
    )
    finish_current(last_time)
    started_at = steps[0]["started_at"] if steps else first_time or ""
    finished_at = (
        last_time
        if last_message_was_dropped_empty_attempt and last_time
        else steps[-1]["finished_at"]
        if steps
        else last_time or started_at
    )
    return {
        "schema_version": "ACTF_v1.0",
        "system_prompt": system_prompt,
        "tools": deepcopy(tools) if tools is not None else None,
        "steps": steps,
        "started_at": started_at,
        "finished_at": finished_at,
    }


def convert_openclaw_trace(
    source: Path,
    destination: Path,
    *,
    user_messages: Iterable[str] | None = None,
    cache_trace: Path | None = None,
    tools: list[Any] | None = None,
    keep_thinking: bool = True,
) -> dict[str, Any]:
    system_prompts = _cache_trace_system_prompts(cache_trace) if cache_trace is not None else []
    trajectory = openclaw_events_to_actf(
        _read_events(source),
        user_messages=user_messages,
        system_prompts=system_prompts,
        tools=tools,
        keep_thinking=keep_thinking,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(
        json.dumps(trajectory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(destination)
    return trajectory
