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

from roller.capture import _safe_file, read_jsonl
from roller.claude_code_actf import _read_events as read_claude_events
from roller.opencode_actf import root_session_ids
from roller.sft import SFTConversionError, _arguments_json, _normalize_tool_call, validate_sft_record
from roller.storage import atomic_write_json

SFT_SEGMENTS_DIR = "sft_segments"


def _is_main_compaction(row: Mapping[str, Any], harness: str) -> bool:
    if harness == "claude-code":
        return row.get("subtype") == "compact_boundary" and not row.get("isSidechain")
    return harness == "codex" and row.get("type") == "compacted"


def _native_rows(path: Path, harness: str) -> list[dict[str, Any]]:
    return read_claude_events(path) if harness == "claude-code" else read_jsonl(path)


def _comparable(value: Any) -> Any:
    """Ignore cache hints while comparing the *same* native representation."""
    if isinstance(value, Mapping):
        return {key: _comparable(item) for key, item in value.items() if key != "cache_control"}
    if isinstance(value, list):
        return [_comparable(item) for item in value]
    return value


def _prefix_changed(inputs: list[list[Any]]) -> bool:
    previous: list[Any] = []
    for messages in inputs:
        current = _comparable(messages)
        if previous and current[:len(previous)] != previous:
            return True
        previous = current
    return False


def _single_opencode_session(rows: list[dict[str, Any]], roots: set[str]) -> bool:
    """Allow untagged requests only when native evidence shows one root session."""
    if len(roots) != 1:
        return False
    sessions = set()
    for row in rows:
        info = None
        parts = []
        if row.get("type") == "session_export":
            data = row.get("data") or {}
            info = data.get("info")
            parts = [part for message in data.get("messages", [])
                     for part in message.get("parts", [])]
        elif row.get("type") == "event":
            event = row.get("event") or {}
            properties = event.get("properties") or {}
            if event.get("type") in {"session.created", "session.updated"}:
                info = properties.get("info")
            elif event.get("type") == "message.part.updated":
                parts = [properties.get("part") or {}]
        if isinstance(info, Mapping) and isinstance(info.get("id"), str):
            sessions.add(info["id"])
        if any(part.get("type") == "tool" and part.get("tool") == "task" for part in parts):
            return False
    return sessions == roots


def has_main_compaction(path: Path, harness: str | None,
                        rows: list[dict[str, Any]] | None = None) -> bool:
    """Whether native evidence rules out a safe linear SFT export."""
    rows = rows if rows is not None else _native_rows(path, harness or "")
    if harness in {"claude-code", "codex"}:
        return any(_is_main_compaction(row, harness) for row in rows)
    if harness == "openclaw":
        return any(row.get("type") == "compaction" for row in rows)
    if harness == "pi" and any(row.get("type") == "event"
                               and (row.get("event") or {}).get("type") == "compaction_end"
                               for row in rows):
        return True
    requests = [row for row in rows if row.get("type") == "request"]
    roots = root_session_ids(rows) if harness == "opencode" else set()
    session_ids = {row.get("native_session_id") for row in requests
                   if isinstance(row.get("native_session_id"), str)}
    # Without session metadata, a single observed session is unambiguous.
    if harness == "opencode" and not roots and len(session_ids) == 1:
        roots = session_ids
    if harness == "opencode" and not roots and len(session_ids) > 1:
        return True  # Cannot establish which session supplied the main prompt.
    if harness == "opencode" and roots and any(
            row.get("native_session_id") is None for row in requests):
        if not _single_opencode_session(rows, roots) or session_ids - roots:
            return True  # An unattributed request may belong to a child.
        roots = set()  # All requests belong to the one observed root.
    inputs = []
    for row in requests:
        if roots and row.get("native_session_id") not in roots:
            continue
        body = row.get("body", row)
        messages = body.get("messages") if isinstance(body, Mapping) else None
        if not isinstance(messages, list):
            continue
        inputs.append(messages)
    return _prefix_changed(inputs)


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
    return _normalize_tool_call({"id": identifier, "name": name, "arguments": arguments},
                                "captured tool call")


def _anthropic_message(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    role = message.get("role")
    content = message.get("content", [])
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        raise SFTConversionError("Anthropic message content is invalid")
    if role == "system":
        return [{"role": "system", "content": _text_blocks(content, kinds={"text"})}]
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
                calls.append(_tool_call(block.get("id"), block.get("name"), block.get("input", {})))
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
                          item.get("input") if tool_kind == "custom" else item.get("arguments", {}), tool_kind)
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
        # Opaque reasoning in a later prompt cannot be reconstructed as text.
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
            calls.append(_tool_call(call.get("id"), function.get("name"), function.get("arguments", {})))
        return {"role": role, "reasoning_content": reasoning, "content": value, "tool_calls": calls}
    return {"role": role, "content": value}


def _prompt_from_call(call: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    context = call.get("context")
    if not isinstance(context, Mapping):
        raise SFTConversionError("captured Call lacks context")
    protocol = call.get("protocol")
    messages: list[dict[str, Any]] = []
    tools_value = context.get("tools", [])
    if not isinstance(tools_value, list):
        raise SFTConversionError("captured tools are invalid")
    if protocol == "anthropic-messages":
        system = context.get("system", "")
        if system:
            messages.append({"role": "system", "content": _text_blocks(system, kinds={"text"})})
        items = context.get("messages", [])
        if not isinstance(items, list):
            raise SFTConversionError("Anthropic request messages are invalid")
        for item in items:
            if not isinstance(item, Mapping):
                raise SFTConversionError("Anthropic request has an invalid message")
            messages.extend(_anthropic_message(item))
        tools = []
        for tool in tools_value:
            if not isinstance(tool, Mapping) or not isinstance(tool.get("name"), str):
                raise SFTConversionError("Anthropic tool schema is invalid")
            tools.append({"type": "function", "function": {"name": tool["name"],
                         "description": tool.get("description", ""),
                         "parameters": deepcopy(tool.get("input_schema", {}))}})
    elif protocol == "openai-responses":
        instructions = context.get("instructions")
        if isinstance(instructions, str) and instructions:
            messages.append({"role": "system", "content": instructions})
        items = context.get("input", [])
        if not isinstance(items, list):
            raise SFTConversionError("Responses request input is invalid")
        for item in items:
            if not isinstance(item, Mapping):
                raise SFTConversionError("Responses request has an invalid item")
            messages.extend(_responses_item(item))
        tools = deepcopy(tools_value)
    elif protocol == "openai-chat-completions":
        items = context.get("messages", [])
        if not isinstance(items, list):
            raise SFTConversionError("Chat Completions request messages are invalid")
        for item in items:
            if not isinstance(item, Mapping):
                raise SFTConversionError("Chat Completions request has an invalid message")
            messages.append(_chat_message(item))
        tools = deepcopy(tools_value)
    else:
        raise SFTConversionError(f"unsupported compaction Call protocol: {protocol}")
    if not isinstance(tools, list):
        raise SFTConversionError("captured tools are invalid")
    return messages, tools


def _target_messages_from_call(call: Mapping[str, Any]) -> list[dict[str, Any]]:
    output = call.get("output")
    if not isinstance(output, Mapping):
        raise SFTConversionError("captured Call lacks output")
    if call.get("protocol") == "anthropic-messages":
        return _anthropic_message({"role": "assistant", "content": output.get("content", [])})
    if call.get("protocol") == "openai-responses":
        result: list[dict[str, Any]] = []
        items = output.get("output", [])
        if not isinstance(items, list):
            raise SFTConversionError("Responses output is invalid")
        for item in items:
            if not isinstance(item, Mapping):
                raise SFTConversionError("Responses output contains an invalid item")
            if item.get("type") == "reasoning":
                # The Responses API may return encrypted reasoning. It is not
                # a supervised text target. A later request containing it is
                # rejected because the actual prompt cannot be represented.
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


def _raw_context_changed(calls: list[dict[str, Any]]) -> bool:
    """Detect a prompt rewrite without requiring SFT conversion of its content."""
    inputs = []
    for call in calls:
        if call.get("export_status") != "available":
            continue
        context = call.get("context")
        messages = context.get("messages") if isinstance(context, Mapping) else None
        if isinstance(messages, list):
            inputs.append(messages)
    return _prefix_changed(inputs)


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
        name, arguments = function.get("name"), function.get("arguments", {})
        if not isinstance(name, str) or not name:
            raise SFTConversionError("ACTF tool call lacks a name")
        encoded = _arguments_json(arguments, "ACTF tool arguments")
        tools.append((name, json.dumps(json.loads(encoded), ensure_ascii=False, sort_keys=True)))
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
        if len(selected) < len(targets) and _assistant_signature(_target_messages_from_call(call)[0]) == targets[len(selected)]:
            selected.append(call)
    if len(selected) != len(targets):
        raise SFTConversionError("captured root Calls cannot be bound to every ACTF main step")
    selected_ids = {call["call_id"] for call in selected}
    for index, call in enumerate(calls):
        if call.get("export_status") != "available" or call["call_id"] in selected_ids:
            continue
        auxiliary = _target_messages_from_call(call)[0]
        if _assistant_signature(auxiliary) in targets:
            raise SFTConversionError("ambiguous root Call matches an ACTF main step")
        summary = auxiliary["content"]
        later_inputs = (_prompt_from_call(later)[0] for later in calls[index + 1:]
                        if later["call_id"] in selected_ids)
        summary_reused = any(
            summary in message["content"]
            for messages in later_inputs for message in messages
            if message["role"] in {"system", "developer", "user"}
            and isinstance(message.get("content"), str)
        )
        if auxiliary["tool_calls"] or not summary.strip() or not summary_reused:
            raise SFTConversionError("unmatched root Call is not bound as a compaction summary")
    return selected


def _compaction_boundaries(harness: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    boundaries = []
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
                    break
        boundaries.append({"event_id": event_id, "timestamp": row.get("timestamp"),
                           "summary_response_id": summary_id})
    return boundaries


def _root_calls(calls: list[dict[str, Any]], manifest: Mapping[str, Any],
                summary_ids: set[str], harness: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    participants = manifest.get("participants")
    if not isinstance(participants, list):
        raise SFTConversionError("capture manifest lacks participants")
    roots = {p.get("participant_id") for p in participants
             if isinstance(p, Mapping) and p.get("role") == "root"}
    if len(roots) != 1 or not all(isinstance(root, str) and root for root in roots):
        raise SFTConversionError("compaction requires one observed root participant")
    summary_calls = {}
    for summary_id in summary_ids:
        matches = [call for call in calls if any(
            isinstance(item, Mapping) and item.get("id") == summary_id
            for item in (call.get("output") or {}).get("output", []))]
        if len(matches) != 1:
            raise SFTConversionError("Codex summary output does not identify one captured Call")
        summary_calls[summary_id] = matches[0]
    summary_contexts = [call.get("context") for call in summary_calls.values()]
    main = []
    for call in sorted(calls, key=lambda c: c.get("request_seq", 0)):
        if call.get("participant_id") not in roots or call in summary_calls.values():
            continue
        if harness == "codex" and call.get("context") in summary_contexts:
            continue  # Failed/retried summary attempts have the same input.
        main.append(call)
    return main, summary_calls


def _segment_records(calls: list[dict[str, Any]], boundaries: list[dict[str, Any]],
                     summary_calls: Mapping[str, dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Use one continuation rule; window_index counts emitted SFT records."""
    times = [_time(boundary["timestamp"]) for boundary in boundaries]
    if any(not isinstance(boundary["event_id"], str) or not boundary["event_id"]
           for boundary in boundaries):
        raise SFTConversionError("compaction boundary lacks a native event ID")
    if any(later <= earlier for earlier, later in zip(times, times[1:])):
        raise SFTConversionError("compaction boundaries are not in chronological order")
    exported: list[tuple[dict[str, Any], dict[str, Any]]] = []
    native_index = -1
    for call in calls:
        if call.get("export_status") != "available":
            raise SFTConversionError(f"main Call {call.get('call_id')} is incomplete")
        when = _time(call.get("started_at")) if boundaries else None
        if when in times:
            raise SFTConversionError("main Call starts at an ambiguous compaction boundary")
        current_native = sum(when > boundary for boundary in times) if when else 0
        prompt, tools = _prompt_from_call(call)
        target = _target_messages_from_call(call)
        if not prompt:
            raise SFTConversionError("compaction segment lacks its actual input")
        previous = exported[-1][1] if exported else None
        prefix = previous["messages"] if previous else []
        continuation = (previous is not None and current_native == native_index
                        and tools == previous["tools"] and prompt[:len(prefix)] == prefix
                        and not any(item["role"] == "assistant" for item in prompt[len(prefix):]))
        if continuation:
            previous["messages"].extend(prompt[len(prefix):] + target)
            exported[-1][0]["last_call_id"] = call["call_id"]
        else:
            boundary = boundaries[current_native - 1] if current_native > native_index and current_native else None
            summary_id = boundary["summary_response_id"] if boundary else None
            metadata = {"path": f"{SFT_SEGMENTS_DIR}/{len(exported)+1:04d}.json",
                        "window_index": len(exported),
                        "boundary_source": ("native_boundary" if boundary else
                                            "observed_context_change" if exported else "initial"),
                        "boundary_event_id": boundary["event_id"] if boundary else None,
                        "summary_call_id": summary_calls[summary_id]["call_id"] if summary_id else None,
                        "summary_call_binding": "native_response_id" if summary_id else
                                                "unattributed" if boundary else None,
                        "first_call_id": call["call_id"], "last_call_id": call["call_id"]}
            record = {"messages": prompt + target, "tools": tools,
                      "target_start_index": len(prompt)}
            # Only assistant messages at/after this index enter the loss;
            # intervening user/tool messages are context for later targets.
            exported.append((metadata, record))
        native_index = current_native
    if not exported:
        raise SFTConversionError("compaction has no complete main-agent Calls")
    for _, record in exported:
        validate_sft_record(record)
    return exported


def build_compaction_segments(sample_dir: Path, harness: str,
                              manifest_relative: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Build verified main-agent windows without changing the sample directory."""
    rows = _native_rows(sample_dir / "trace.jsonl", harness)
    boundaries = _compaction_boundaries(harness, rows)
    native_reset = has_main_compaction(sample_dir / "trace.jsonl", harness, rows)
    if harness in {"claude-code", "codex"} and not boundaries:
        return []
    summary_ids = {boundary["summary_response_id"] for boundary in boundaries
                   if boundary["summary_response_id"]}
    if harness == "codex" and len(summary_ids) != len(boundaries):
        raise SFTConversionError("Codex summary output cannot be bound to every compaction boundary")
    capture_dir = _safe_file(sample_dir, manifest_relative).parent
    calls = read_jsonl(capture_dir / "calls.jsonl")
    manifest = json.loads((capture_dir / "manifest.json").read_text(encoding="utf-8"))
    main, summary_calls = _root_calls(calls, manifest, summary_ids, harness)
    if not boundaries:
        if not main or any(call.get("protocol") != "openai-chat-completions" for call in main):
            if native_reset:
                raise SFTConversionError("native context change lacks usable root Calls")
            return []
        if not native_reset and not _raw_context_changed(main):
            return []  # Preserve the existing linear SFT path, including images.
        main = _main_chat_calls(sample_dir, main)
        if not native_reset and not _raw_context_changed(main):
            return []  # Only an auxiliary request changed its context.
    elif harness == "claude-code" and (sample_dir / "trace_ACTF.json").is_file():
        # Claude may attribute the summary request to root. ACTF identifies
        # the main outputs; unmatched root output must be reused as a summary.
        main = _main_chat_calls(sample_dir, main)
    return _segment_records(main, boundaries, summary_calls)


def export_compaction_segments(sample_dir: Path, harness: str,
                               manifest_relative: str) -> list[dict[str, Any]]:
    """Write SFT windows only after every window has passed validation."""
    segments = build_compaction_segments(sample_dir, harness, manifest_relative)
    for metadata, record in segments:
        atomic_write_json(sample_dir / metadata["path"], record)
    return [metadata for metadata, _ in segments]
