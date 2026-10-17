"""SFT windows reconstructed from captured main-agent requests.

Native events identify Claude/Codex compaction and its summary call. For other
harnesses, a request that no longer extends the verified context starts a new
window. The request body establishes what the model actually saw.
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from roller.capture import read_jsonl
from roller.claude_code_actf import _read_events as read_claude_events
from roller.sft import SFTConversionError, validate_sft_record
from roller.storage import atomic_write_json

SFT_SEGMENTS_DIR = "sft_segments"


def _is_main_compaction(row: Mapping[str, Any], harness: str) -> bool:
    if harness == "claude-code":
        return row.get("subtype") == "compact_boundary" and not row.get("isSidechain")
    return harness == "codex" and row.get("type") == "compacted"


def _native_rows(path: Path, harness: str) -> list[dict[str, Any]]:
    return read_claude_events(path) if harness == "claude-code" else read_jsonl(path)


def has_main_compaction(path: Path, harness: str | None) -> bool:
    """Whether native evidence rules out a safe linear SFT export."""
    rows = _native_rows(path, harness or "")
    if harness in {"claude-code", "codex"}:
        return any(_is_main_compaction(row, harness) for row in rows)
    if harness == "openclaw":
        return any(row.get("type") == "compaction" for row in rows)
    if harness == "pi" and any(row.get("type") == "event"
                               and (row.get("event") or {}).get("type") == "compaction_end"
                               for row in rows):
        return True
    previous: list[Any] = []
    for row in rows:
        if row.get("type") != "request":
            continue
        body = row.get("body", row)
        messages = body.get("messages") if isinstance(body, Mapping) else None
        if not isinstance(messages, list):
            continue
        if previous and messages[:len(previous)] != previous:
            return True
        previous = messages
    return False


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise SFTConversionError("compaction or call lacks a timestamp")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SFTConversionError(f"invalid compaction timestamp: {value}") from exc
    if timestamp.tzinfo is None:
        raise SFTConversionError(f"compaction timestamp lacks timezone: {value}")
    return timestamp


def _text_blocks(content: Any, *, kinds: set[str]) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise SFTConversionError("model input contains unsupported content")
    texts = []
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") not in kinds or not isinstance(block.get("text"), str):
            raise SFTConversionError("model input contains non-text content")
        texts.append(block["text"])
    return "\n".join(texts)


def _tool_call(identifier: Any, name: Any, arguments: Any, kind: str = "function") -> dict[str, Any]:
    if not isinstance(identifier, str) or not identifier or not isinstance(name, str) or not name:
        raise SFTConversionError("captured tool call lacks an ID or name")
    if kind == "custom":
        if not isinstance(arguments, str):
            raise SFTConversionError("captured custom tool input is not text")
        return {"id": identifier, "type": "custom", "custom": {"name": name, "input": arguments}}
    try:
        value = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(value, dict):
            raise ValueError("not an object")
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (ValueError, TypeError) as exc:
        raise SFTConversionError("captured tool arguments are not a JSON object") from exc
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": encoded}}


def _anthropic_message(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    role = message.get("role")
    content = message.get("content", [])
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        raise SFTConversionError("Anthropic message content is invalid")
    if role == "assistant":
        visible, reasoning, calls = [], [], []
        for block in content:
            if not isinstance(block, Mapping):
                raise SFTConversionError("Anthropic assistant block is invalid")
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                visible.append(block["text"])
            elif kind == "thinking" and isinstance(block.get("thinking"), str):
                reasoning.append(block["thinking"])
            elif kind == "tool_use":
                calls.append(_tool_call(block.get("id"), block.get("name"), block.get("input")))
            elif kind == "redacted_thinking":
                raise SFTConversionError("redacted Anthropic thinking cannot be represented in SFT")
            else:
                raise SFTConversionError(f"unsupported Anthropic assistant block: {kind}")
        return [{"role": "assistant", "reasoning_content": "\n\n".join(reasoning),
                 "content": "\n\n".join(visible), "tool_calls": calls}]
    if role != "user":
        raise SFTConversionError(f"unsupported Anthropic role: {role}")
    result: list[dict[str, Any]] = []
    text: list[str] = []
    def flush() -> None:
        if text:
            result.append({"role": "user", "content": "\n".join(text)})
            text.clear()
    for block in content:
        if not isinstance(block, Mapping):
            raise SFTConversionError("Anthropic user block is invalid")
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text.append(block["text"])
        elif block.get("type") == "tool_result":
            flush()
            identifier = block.get("tool_use_id")
            if not isinstance(identifier, str) or not identifier:
                raise SFTConversionError("Anthropic tool result lacks an ID")
            result.append({"role": "tool", "tool_call_id": identifier,
                           "content": _text_blocks(block.get("content", ""), kinds={"text"})})
        else:
            raise SFTConversionError(f"unsupported Anthropic user block: {block.get('type')}")
    flush()
    return result


def _responses_item(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    kind = item.get("type")
    if kind == "message":
        role = item.get("role")
        if role not in {"system", "developer", "user", "assistant"}:
            raise SFTConversionError(f"unsupported Responses role: {role}")
        value = _text_blocks(item.get("content", []), kinds={"input_text", "output_text"})
        if role == "assistant":
            return [{"role": role, "reasoning_content": "", "content": value, "tool_calls": []}]
        return [{"role": role, "content": value}]
    if kind in {"function_call", "custom_tool_call"}:
        tool_kind = "custom" if kind == "custom_tool_call" else "function"
        call = _tool_call(item.get("call_id"), item.get("name"),
                          item.get("input") if tool_kind == "custom" else item.get("arguments"), tool_kind)
        return [{"role": "assistant", "reasoning_content": "", "content": "", "tool_calls": [call]}]
    if kind in {"function_call_output", "custom_tool_call_output"}:
        identifier = item.get("call_id")
        if not isinstance(identifier, str) or not identifier:
            raise SFTConversionError("Responses tool output lacks a call ID")
        output = item.get("output")
        if not isinstance(output, str):
            raise SFTConversionError("Responses tool output is not text")
        return [{"role": "tool", "tool_call_id": identifier, "content": output}]
    if kind == "reasoning":
        # Encrypted reasoning has no trainable text. Keep the visible response
        # while rejecting inputs whose reasoning content cannot be represented.
        raise SFTConversionError("Responses reasoning item cannot be represented in SFT")
    raise SFTConversionError(f"unsupported Responses input item: {kind}")


def _chat_message(message: Mapping[str, Any]) -> dict[str, Any]:
    role = message.get("role")
    if role not in {"system", "developer", "user", "assistant", "tool"}:
        raise SFTConversionError(f"unsupported Chat Completions role: {role}")
    content = message.get("content")
    if content is None and role == "assistant":
        content = ""
    value = _text_blocks(content, kinds={"text"})
    if role == "tool":
        identifier = message.get("tool_call_id")
        if not isinstance(identifier, str) or not identifier:
            raise SFTConversionError("Chat Completions tool result lacks a call ID")
        return {"role": role, "tool_call_id": identifier, "content": value}
    if role == "assistant":
        reasoning = message.get("reasoning_content") or ""
        if not isinstance(reasoning, str):
            raise SFTConversionError("Chat Completions reasoning is not text")
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise SFTConversionError("Chat Completions tool calls are invalid")
        calls = []
        for call in raw_calls:
            if not isinstance(call, Mapping) or not isinstance(call.get("function"), Mapping):
                raise SFTConversionError("Chat Completions tool call is invalid")
            function = call["function"]
            calls.append(_tool_call(call.get("id"), function.get("name"), function.get("arguments")))
        return {"role": role, "reasoning_content": reasoning, "content": value, "tool_calls": calls}
    return {"role": role, "content": value}


def _request(call: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    context = call.get("context")
    if not isinstance(context, Mapping):
        raise SFTConversionError("captured Call lacks context")
    protocol = call.get("protocol")
    messages: list[dict[str, Any]] = []
    if protocol == "anthropic-messages":
        system = context.get("system", "")
        if system:
            messages.append({"role": "system", "content": _text_blocks(system, kinds={"text"})})
        for item in context.get("messages", []):
            if not isinstance(item, Mapping):
                raise SFTConversionError("Anthropic request has an invalid message")
            messages.extend(_anthropic_message(item))
        tools = []
        for tool in context.get("tools", []):
            if not isinstance(tool, Mapping) or not isinstance(tool.get("name"), str):
                raise SFTConversionError("Anthropic tool schema is invalid")
            tools.append({"type": "function", "function": {"name": tool["name"],
                         "description": tool.get("description", ""),
                         "parameters": deepcopy(tool.get("input_schema", {}))}})
    elif protocol == "openai-responses":
        instructions = context.get("instructions")
        if isinstance(instructions, str) and instructions:
            messages.append({"role": "system", "content": instructions})
        for item in context.get("input", []):
            if not isinstance(item, Mapping):
                raise SFTConversionError("Responses request has an invalid item")
            messages.extend(_responses_item(item))
        tools = deepcopy(context.get("tools", []))
    elif protocol == "openai-chat-completions":
        for item in context.get("messages", []):
            if not isinstance(item, Mapping):
                raise SFTConversionError("Chat Completions request has an invalid message")
            messages.append(_chat_message(item))
        tools = deepcopy(context.get("tools", []))
    else:
        raise SFTConversionError(f"unsupported compaction Call protocol: {protocol}")
    if not isinstance(tools, list):
        raise SFTConversionError("captured tools are invalid")
    return messages, tools


def _response(call: Mapping[str, Any]) -> list[dict[str, Any]]:
    output = call.get("output")
    if not isinstance(output, Mapping):
        raise SFTConversionError("captured Call lacks output")
    if call.get("protocol") == "anthropic-messages":
        return _anthropic_message({"role": "assistant", "content": output.get("content", [])})
    if call.get("protocol") == "openai-responses":
        result: list[dict[str, Any]] = []
        for item in output.get("output", []):
            if not isinstance(item, Mapping):
                raise SFTConversionError("Responses output contains an invalid item")
            if item.get("type") == "reasoning":
                # The Responses API may return encrypted reasoning. It is not
                # a supervised text target; the next request is checked in
                # full and rejected if it depends on that opaque item.
                continue
            result.extend(_responses_item(item))
        if not result or any(message["role"] != "assistant" for message in result):
            raise SFTConversionError("Responses output has no representable assistant target")
        return result
    if call.get("protocol") == "openai-chat-completions":
        choices = output.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise SFTConversionError("Chat Completions output lacks one choice")
        message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            raise SFTConversionError("Chat Completions output lacks an assistant target")
        return [_chat_message(message)]
    raise SFTConversionError("unsupported compaction response protocol")


def _call_windows(calls: list[dict[str, Any]], *, force: bool = False) -> list[list[dict[str, Any]]]:
    """Group root Calls while their inputs extend the previously verified context."""
    windows: list[list[dict[str, Any]]] = []
    messages: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    previous_input: list[dict[str, Any]] = []
    previous_tools: list[dict[str, Any]] = []
    context_changed = False
    for call in calls:
        if call.get("export_status") != "available":
            continue  # A failed attempt has no supervised model output.
        actual, actual_tools = _request(call)
        if not actual:
            raise SFTConversionError("captured Call lacks its actual input")
        if previous_input and (actual[:len(previous_input)] != previous_input
                               or actual_tools != previous_tools):
            context_changed = True
        if windows and (actual_tools != tools or actual[:len(messages)] != messages
                        or any(item["role"] == "assistant" for item in actual[len(messages):])):
            windows.append([])
        elif not windows:
            windows.append([])
        windows[-1].append(call)
        previous_input, previous_tools = actual, actual_tools
        messages, tools = actual + _response(call), actual_tools
    if not context_changed and not force:
        return []
    if len(windows) > 1 and any(call.get("export_status") != "available" for call in calls):
        raise SFTConversionError("context changed while a main Call is incomplete")
    return windows


def _assistant_signature(message: Mapping[str, Any]) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Compare model outputs with ACTF main steps without harness-specific tool IDs."""
    content = message.get("content")
    calls = message.get("tool_calls") or []
    if not isinstance(content, str) or not isinstance(calls, list):
        raise SFTConversionError("ACTF assistant output cannot be matched to a Call")
    tools = []
    for call in calls:
        if not isinstance(call, Mapping):
            raise SFTConversionError("ACTF tool call cannot be matched to a Call")
        function = call.get("function") if call.get("type") == "function" else call
        if not isinstance(function, Mapping):
            raise SFTConversionError("ACTF tool call cannot be matched to a Call")
        name, arguments = function.get("name"), function.get("arguments")
        if not isinstance(name, str) or not name:
            raise SFTConversionError("ACTF tool call lacks a name")
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
            if not isinstance(parsed, dict):
                raise ValueError("tool arguments are not an object")
            tools.append((name, json.dumps(parsed, ensure_ascii=False, sort_keys=True)))
        except (ValueError, TypeError) as exc:
            raise SFTConversionError("ACTF tool arguments cannot be matched to a Call") from exc
    return content, tuple(tools)


def _main_chat_calls(sample_dir: Path, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exclude auxiliary model requests absent from the executed ACTF main trace."""
    trajectory = json.loads((sample_dir / "trace_ACTF.json").read_text(encoding="utf-8"))
    steps = trajectory.get("steps") if isinstance(trajectory, Mapping) else None
    if not isinstance(steps, list) or not steps or any(not isinstance(step, Mapping) for step in steps):
        raise SFTConversionError("main ACTF steps are required to bind captured Calls")
    targets = [_assistant_signature(step.get("assistant_content", {})) for step in steps]
    selected: list[dict[str, Any]] = []
    for call in calls:
        if call.get("export_status") != "available":
            continue
        if len(selected) < len(targets) and _assistant_signature(_response(call)[0]) == targets[len(selected)]:
            selected.append(call)
    if len(selected) != len(targets):
        raise SFTConversionError("captured root Calls cannot be bound to every ACTF main step")
    selected_ids = {call["call_id"] for call in selected}
    for index, call in enumerate(calls):
        if call.get("export_status") != "available" or call["call_id"] in selected_ids:
            continue
        auxiliary = _response(call)[0]
        if _assistant_signature(auxiliary) in targets:
            raise SFTConversionError("ambiguous root Call matches an ACTF main step")
        summary = auxiliary["content"]
        later_inputs = (_request(later)[0] for later in calls[index + 1:]
                        if later["call_id"] in selected_ids)
        summary_reused = any(
            summary in message["content"]
            for messages in later_inputs for message in messages
            if isinstance(message.get("content"), str)
        )
        if auxiliary["tool_calls"] or not summary.strip() or not summary_reused:
            raise SFTConversionError("unmatched root Call is not bound as a compaction summary")
    return selected


def _boundaries(harness: str, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], set[str]]:
    boundaries, summary_ids = [], set()
    for index, row in enumerate(rows):
        if not _is_main_compaction(row, harness):
            continue
        summary_id = None
        if harness == "claude-code":
            event_id = row.get("uuid")
        else:
            event_id = (row.get("payload") or {}).get("window_id")
            # The immediately preceding native assistant output is the summary,
            # even when previous HTTP attempts failed and were retried.
            for previous in reversed(rows[:index]):
                payload = previous.get("payload") or {}
                if previous.get("type") == "response_item" and payload.get("role") == "assistant":
                    summary = "\n".join(part.get("text", "") for part in payload.get("content", [])
                                        if isinstance(part, Mapping) and isinstance(part.get("text"), str))
                    compact_text = (row.get("payload") or {}).get("message")
                    if (isinstance(payload.get("id"), str) and summary
                            and isinstance(compact_text, str) and summary in compact_text):
                        summary_id = payload["id"]
                        summary_ids.add(summary_id)
                    break
        boundaries.append({"event_id": event_id, "timestamp": row.get("timestamp"),
                           "summary_response_id": summary_id})
    return boundaries, summary_ids


def build_compaction_segments(sample_dir: Path, harness: str,
                              manifest_relative: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Build verified main-agent windows without changing the sample directory."""
    rows = _native_rows(sample_dir / "trace.jsonl", harness)
    boundaries, summary_ids = _boundaries(harness, rows)
    if harness == "codex" and len(summary_ids) != len(boundaries):
        raise SFTConversionError("Codex summary output cannot be bound to every compaction boundary")
    capture_dir = (sample_dir / manifest_relative).parent
    calls = read_jsonl(capture_dir / "calls.jsonl")
    manifest = json.loads((capture_dir / "manifest.json").read_text(encoding="utf-8"))
    roots = {p["participant_id"] for p in manifest.get("participants", []) if p.get("role") == "root"}
    if len(roots) != 1:
        raise SFTConversionError("compaction requires one observed root participant")
    root = next(iter(roots))
    summary_calls = {summary_id: [call for call in calls
                     if any(isinstance(item, Mapping) and item.get("id") == summary_id
                            for item in (call.get("output") or {}).get("output", []))]
                     for summary_id in summary_ids}
    if any(len(matches) != 1 for matches in summary_calls.values()):
        raise SFTConversionError("Codex summary output does not identify one captured Call")
    summary_contexts = [matches[0].get("context") for matches in summary_calls.values()]
    boundary_times = [_time(b["timestamp"]) for b in boundaries]
    if any(not isinstance(b["event_id"], str) or not b["event_id"] for b in boundaries):
        raise SFTConversionError("compaction boundary lacks a native event ID")
    if any(later <= earlier for earlier, later in zip(boundary_times, boundary_times[1:])):
        raise SFTConversionError("compaction boundaries are not in chronological order")
    root_calls: list[dict[str, Any]] = []
    for call in sorted(calls, key=lambda c: c.get("request_seq", 0)):
        if call.get("participant_id") != root:
            continue
        output_items = (call.get("output") or {}).get("output", [])
        if any(isinstance(item, Mapping) and item.get("id") in summary_ids for item in output_items):
            continue
        if harness == "codex" and any(call.get("context") == context for context in summary_contexts):
            # Failed/retried summary requests use the same synthetic input.
            continue
        root_calls.append(call)
    if not boundaries:
        if not root_calls or any(call.get("protocol") != "openai-chat-completions" for call in root_calls):
            if has_main_compaction(sample_dir / "trace.jsonl", harness):
                raise SFTConversionError("native context change lacks usable root Calls")
            return []
        observed_windows = _call_windows(root_calls)
        if not observed_windows and not has_main_compaction(sample_dir / "trace.jsonl", harness):
            return []
        root_calls = _main_chat_calls(sample_dir, root_calls)
        windows = _call_windows(root_calls, force=True)
    else:
        windows = [[] for _ in range(len(boundaries) + 1)]
    for call in root_calls if boundaries else []:
        when = _time(call.get("started_at"))
        if when in boundary_times:
            raise SFTConversionError("main Call starts at an ambiguous compaction boundary")
        window = sum(when > boundary for boundary in boundary_times)
        if call.get("export_status") != "available":
            raise SFTConversionError(f"main Call {call.get('call_id')} is incomplete")
        windows[window].append(call)
    if not any(windows):
        raise SFTConversionError("compaction has no complete main-agent Calls")
    exported: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for window_index, window_calls in enumerate(windows):
        if not window_calls:
            continue
        messages, tools = _request(window_calls[0])
        target_start = len(messages)
        if not messages:
            raise SFTConversionError("compaction segment lacks its actual input")
        for index, call in enumerate(window_calls):
            actual, actual_tools = _request(call)
            if actual_tools != tools:
                raise SFTConversionError("tools changed within one compaction window")
            if index:
                if actual[:len(messages)] != messages:
                    raise SFTConversionError("Call input does not extend the preceding context in its compaction window")
                added = actual[len(messages):]
                if any(message["role"] == "assistant" for message in added):
                    raise SFTConversionError("Call input contains an unverified assistant message")
                messages.extend(added)
            messages.extend(_response(call))
        record = {"messages": messages, "tools": tools, "target_start_index": target_start}
        validate_sft_record(record)
        filename = f"{SFT_SEGMENTS_DIR}/{len(exported)+1:04d}.json"
        metadata = {"path": filename, "window_index": window_index,
                    "boundary_event_id": boundaries[window_index-1]["event_id"] if window_index and boundaries else None,
                    "first_call_id": window_calls[0]["call_id"],
                    "last_call_id": window_calls[-1]["call_id"]}
        if window_index and boundaries:
            summary_id = boundaries[window_index-1]["summary_response_id"]
            metadata["summary_call_id"] = (summary_calls[summary_id][0]["call_id"]
                                           if summary_id else None)
            metadata["summary_call_binding"] = ("native_response_id" if summary_id
                                                else "unattributed")
        exported.append((metadata, record))
    return exported


def export_compaction_segments(sample_dir: Path, harness: str,
                               manifest_relative: str) -> list[dict[str, Any]]:
    """Write SFT windows only after every window has passed validation."""
    segments = build_compaction_segments(sample_dir, harness, manifest_relative)
    for metadata, record in segments:
        atomic_write_json(sample_dir / metadata["path"], record)
    return [metadata for metadata, _ in segments]
