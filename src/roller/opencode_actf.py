"""Project OpenCode's native session export and request journal into ACTF."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from roller.actf import openclaw_events_to_actf
from roller.storage import atomic_write_json
from roller.usage import ZERO_USAGE


def read_opencode_events(path: Path) -> list[dict[str, Any]]:
    # JSON Lines records are delimited by LF, not every character recognized by
    # str.splitlines().  In particular, U+2028/U+2029 are legal inside a JSON
    # string and must remain part of the record.
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]
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


def _attachment_metadata(value: Any) -> Any:
    """Keep attachment provenance without copying large inline payloads to ACTF.

    OpenCode's ``read`` tool returns images as data URLs in ``attachments``.
    The native JSONL remains the lossless source, while ACTF/SFT gets a bounded,
    auditable description instead of duplicating multi-megabyte base64 strings.
    """

    if isinstance(value, dict):
        return {str(key): _attachment_metadata(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_attachment_metadata(item) for item in value]
    if isinstance(value, str) and len(value) > 4096:
        summary: dict[str, Any] = {
            "omitted": True,
            "characters": len(value),
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        }
        if value.startswith("data:"):
            header, separator, payload = value.partition(",")
            summary.update(
                {
                    "scheme": "data",
                    "header": header,
                    "payload_characters": len(payload) if separator else 0,
                }
            )
        return summary
    return deepcopy(value)


def _attachment_placeholder(attachments: list[Any]) -> str:
    metadata = _attachment_metadata(attachments)
    return "[OpenCode attachments omitted from text-only export: " + json.dumps(
        metadata, ensure_ascii=False, separators=(",", ":")
    ) + "]"


def _tool_result_content(result: str, attachments: list[Any]) -> list[dict[str, Any]]:
    content = [{"type": "text", "text": result}]
    non_image_attachments: list[Any] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise ValueError("OpenCode tool attachment must be an object")
        mime = attachment.get("mime")
        if isinstance(mime, str) and mime.lower().startswith("image/"):
            image_url = attachment.get("url")
            if not isinstance(image_url, str) or not image_url:
                raise ValueError("OpenCode image attachment must contain a URL")
            content.append(
                {"type": "image", "data": image_url, "mimeType": mime.lower()}
            )
        else:
            non_image_attachments.append(attachment)
    if non_image_attachments:
        content.append(
            {
                "type": "text",
                "text": _attachment_placeholder(non_image_attachments),
            }
        )
    return content


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
                attachments = state.get("attachments")
                message: dict[str, Any] = {"role": "toolResult", "toolCallId": call_id, "toolName": name, "content": result, "isError": state["status"] == "error"}
                if attachments:
                    if not isinstance(attachments, list):
                        raise ValueError("OpenCode tool attachments must be a list")
                    metadata = _attachment_metadata(attachments)
                    message["content"] = _tool_result_content(result, attachments)
                    message["details"] = {"attachments": metadata}
                results.append({"type": "message", "timestamp": _timestamp(state.get("time", {}).get("end")), "message": message})
            elif kind in {"step-start", "step-finish", "snapshot", "patch", "retry"}:
                continue  # Execution metadata remains in the native journal.
            else:
                raise ValueError(f"OpenCode part would lose data in text-only SFT: {kind}")
        visible_content = any(
            part.get("type") == "toolCall"
            or (part.get("type") == "text" and bool(part.get("text")))
            or (
                keep_thinking
                and part.get("type") == "thinking"
                and bool(part.get("thinking"))
            )
            for part in content
        )
        if role == "assistant" and not visible_content and not results:
            # Some providers emit a completed reasoning-only turn before the
            # next assistant message. Once private reasoning is stripped that
            # turn cannot be represented as valid SFT. Keep it in the native
            # journal, but omit it from ACTF so a successful rollout does not
            # become an invalid empty assistant message.
            continue
        if (
            role == "assistant"
            and not content
            and not results
            and not info.get("finish")
            and not info.get("time", {}).get("completed")
        ):
            # OpenCode creates the next assistant record before sending the
            # provider request. If Roller reaches its wall-clock timeout while
            # that request is still in flight, the native export can end with
            # this completely empty, unfinished placeholder. Preserve it in
            # the raw journal for diagnosis, but do not turn it into an invalid
            # empty training message.
            continue
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
