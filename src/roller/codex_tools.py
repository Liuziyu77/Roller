"""Capture Codex Responses tool definitions without inferring or merging schemas."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping


def tool_snapshot(body: Mapping[str, Any]) -> dict[str, Any]:
    """Record only tool-related request fields, never message text or headers."""
    snapshot = {
        key: deepcopy(body[key])
        for key in ("tools", "tool_choice", "parallel_tool_calls")
        if key in body
    }
    items = body.get("input")
    snapshot["additional_tools"] = [
        deepcopy(item) for item in items
        if isinstance(item, dict) and item.get("type") == "additional_tools"
    ] if isinstance(items, list) else []
    return snapshot


def read_tool_capture(path: Path | None) -> dict[str, Any]:
    capture: dict[str, Any] = {
        "source": "responses_request", "status": "missing", "requests": [],
    }
    if path is None or not path.is_file():
        capture["error"] = (
            "Codex tool definitions were not captured; use --api-base and "
            "--wire-trace actf/full with HTTP transport"
        )
        return capture
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("kind") == "codex_tools_error":
                    raise ValueError(entry.get("error", "Request recording failed"))
                if entry.get("kind") == "codex_tools":
                    snapshot = entry["snapshot"]
                elif (
                    isinstance(entry.get("body"), dict)
                    and entry.get("method", "POST") == "POST"
                    and str(entry.get("path", "")).split("?")[0].rstrip("/").endswith("/responses")
                ):
                    snapshot = tool_snapshot(entry["body"])
                else:
                    continue
                if not isinstance(snapshot, dict):
                    raise ValueError("tool snapshot must be an object")
                capture["requests"].append({"seq": entry.get("seq"), "snapshot": snapshot})
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        capture.update(status="invalid", error=f"Cannot read Codex tool capture: {exc}")
        return capture
    if not capture["requests"]:
        capture["error"] = "No Responses tool snapshot was recorded"
        return capture
    snapshots = [r["snapshot"] for r in capture["requests"]]
    if any(not isinstance(s.get("tools", []), list) for s in snapshots):
        capture.update(status="invalid", error="Responses tools must be a list")
    elif any(s.get("additional_tools") for s in snapshots):
        # Programmatic tools can contain a different schema language. Keep the
        # exact input items rather than pretending they are function schemas.
        capture.update(
            status="unsupported",
            error="Responses additional_tools items require a programmatic-tool "
            "SFT representation; exact items are retained in tools_capture.requests",
        )
    elif any(s.get("tools", []) != snapshots[0].get("tools", []) for s in snapshots[1:]):
        capture.update(
            status="changed",
            error="Codex tools changed across requests; a single SFT tools list "
            "cannot represent them faithfully",
        )
    else:
        capture["status"] = "captured"
    return capture


def tool_index(tools: Any) -> dict[tuple[str, str], str]:
    """Validate native definitions and index callable names; do not rewrite them."""
    if not isinstance(tools, list):
        raise ValueError("Responses tools must be a list")
    names: dict[tuple[str, str], str] = {}

    def visit(items: list[Any], namespace: str = "") -> None:
        for tool in items:
            if not isinstance(tool, dict) or not isinstance(tool.get("type"), str):
                raise ValueError("Responses tool definition must have a string type")
            kind = tool["type"]
            if kind == "namespace":
                name = tool.get("name")
                children = tool.get("tools")
                if namespace or not isinstance(name, str) or not name or not isinstance(children, list):
                    raise ValueError("Invalid Responses tool namespace")
                visit(children, name)
                continue
            name = tool.get("name") if kind in {"function", "custom"} else kind
            if not isinstance(name, str) or not name:
                raise ValueError("Responses tool definition has no callable name")
            key = (namespace, name)
            if key in names:
                raise ValueError(f"Duplicate Responses tool: {key}")
            names[key] = kind
    visit(tools)
    return names
