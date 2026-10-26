"""Compaction windows must follow captured requests, including mid-turn resets."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from roller.compaction_sft import build_compaction_segments, export_compaction_segments, has_main_compaction
from roller.collector import CollectionConfig, Collector
from roller.integrity import validate_resumable_sample
from roller.models import HarnessResult, RunRequest, Task
from roller.sft import SFTConversionError, validate_sft_record


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _capture(tmp_path: Path, root: str, calls: list[dict]) -> str:
    relative = "capture/attempt-0001/manifest.json"
    capture = tmp_path / "capture/attempt-0001"
    _write(capture / "calls.jsonl", calls)
    (capture / "manifest.json").write_text(json.dumps({
        "participants": [{"participant_id": root, "role": "root"}]
    }), encoding="utf-8")
    return relative


def _call(index: int, root: str | None, protocol: str, context: dict,
          output: dict | None, *, status: str = "available") -> dict:
    return {"call_id": f"call-{index}", "request_seq": index,
            "participant_id": root, "protocol": protocol, "context": context,
            "output": output, "export_status": status,
            "started_at": f"2026-10-15T00:00:{index:02d}Z"}


def _actf(path: Path, *assistants: str | dict) -> None:
    (path / "trace_ACTF.json").write_text(json.dumps({"steps": [
        {"assistant_content": {"content": answer, "tool_calls": []}
         if isinstance(answer, str) else answer}
        for answer in assistants
    ]}), encoding="utf-8")


def test_claude_preserved_assistant_is_prompt_not_target(tmp_path: Path) -> None:
    root = "root:session"
    _actf(tmp_path, "EARLIER_ACK", "ANSWER")
    _write(tmp_path / "trace.jsonl", [
        {"type": "system", "subtype": "compact_boundary", "uuid": "boundary-1",
         "timestamp": "2026-10-15T00:00:03Z", "isSidechain": False},
    ])
    old = {"model": "claude", "system": "before", "tools": [],
           "messages": [{"role": "user", "content": "task"}]}
    resumed = {"model": "claude", "system": "after", "tools": [], "messages": [
        {"role": "user", "content": "summary"},
        {"role": "assistant", "content": "EARLIER_ACK"},
        {"role": "user", "content": "final question"},
    ]}
    calls = [
        _call(1, root, "anthropic-messages", old,
              {"content": [{"type": "text", "text": "EARLIER_ACK"}]}),
        _call(2, None, "anthropic-messages", old,
              {"content": [{"type": "text", "text": "summary"}]}, status="unavailable"),
        _call(4, root, "anthropic-messages", resumed,
              {"content": [{"type": "text", "text": "ANSWER"}]}),
    ]
    manifest = _capture(tmp_path, root, calls)
    segments = export_compaction_segments(tmp_path, "claude-code", manifest)
    assert len(segments) == 2
    assert [item["path"] for item in segments] == ["sft_segments/0001.json", "sft_segments/0002.json"]
    record = json.loads((tmp_path / segments[1]["path"]).read_text())
    validate_sft_record(record)
    assert record["target_start_index"] == 4
    assert record["messages"][2]["content"] == "EARLIER_ACK"
    assert record["messages"][4]["content"] == "ANSWER"
    assert segments[1]["boundary_event_id"] == "boundary-1"
    assert segments[1]["summary_call_id"] is None
    assert segments[1]["summary_call_binding"] == "unattributed"


def test_claude_compacts_during_one_user_tool_turn(tmp_path: Path) -> None:
    root = "root:session"
    _actf(tmp_path, {"content": "", "tool_calls": [
        {"name": "Read", "arguments": {"path": "a"}}]}, "done")
    _write(tmp_path / "trace.jsonl", [
        {"type": "system", "subtype": "compact_boundary", "uuid": "boundary-1",
         "timestamp": "2026-10-15T00:00:02Z", "isSidechain": False,
         "compactMetadata": {"trigger": "manual"}},
    ])
    tool = {"name": "Read", "input_schema": {"type": "object"}}
    first = {"system": "system", "tools": [tool],
             "messages": [{"role": "user", "content": "read the file"}]}
    last = {"system": "system", "tools": [tool], "messages": [
        {"role": "user", "content": "compacted task summary"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "tool-1",
                                              "name": "Read", "input": {"path": "a"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1",
                                         "content": "file contents"}]},
    ]}
    manifest = _capture(tmp_path, root, [
        _call(1, root, "anthropic-messages", first,
              {"content": [{"type": "tool_use", "id": "tool-1", "name": "Read",
                            "input": {"path": "a"}}]}),
        _call(3, root, "anthropic-messages", last,
              {"content": [{"type": "text", "text": "done"}]}),
    ])
    _, resumed = build_compaction_segments(tmp_path, "claude-code", manifest)
    metadata, record = resumed
    assert metadata["first_call_id"] == "call-3"
    assert [m["role"] for m in record["messages"]] == ["system", "user", "assistant", "tool", "assistant"]
    assert record["target_start_index"] == 4
    assert record["messages"][3]["tool_call_id"] == "tool-1"


def test_claude_manual_compaction_preserves_injected_system_messages(tmp_path: Path) -> None:
    _actf(tmp_path, "recent answer", "done")
    _write(tmp_path / "trace.jsonl", [{"type": "system", "subtype": "compact_boundary",
                                        "uuid": "manual-boundary", "timestamp": "2026-10-15T00:00:03Z"}])
    before = {"system": [{"type": "text", "text": "base instructions"}], "tools": [],
              "messages": [{"role": "user", "content": "task"},
                           {"role": "system", "content": "injected instructions"}]}
    after = {"system": before["system"], "tools": [], "messages": [
        {"role": "user", "content": "summary"},
        {"role": "assistant", "content": "recent answer"},
        {"role": "user", "content": "continue"},
        {"role": "system", "content": "post-compaction instructions"},
    ]}
    manifest = _capture(tmp_path, "root", [
        _call(1, "root", "anthropic-messages", before,
              {"content": [{"type": "text", "text": "recent answer"}]}),
        _call(4, "root", "anthropic-messages", after,
              {"content": [{"type": "text", "text": "done"}]}),
    ])
    segments = build_compaction_segments(tmp_path, "claude-code", manifest)
    assert len(segments) == 2
    record = segments[1][1]
    assert [message["role"] for message in record["messages"]] == [
        "system", "user", "assistant", "user", "system", "assistant"]
    assert record["target_start_index"] == 5
    assert record["messages"][4]["content"] == "post-compaction instructions"


def test_codex_summary_call_is_not_a_training_target(tmp_path: Path) -> None:
    root = "thread:root"
    _actf(tmp_path, "ack", "done")
    _write(tmp_path / "trace.jsonl", [
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                             "id": "summary-msg", "content": [
                                                 {"type": "output_text", "text": "summary"}]}},
        {"type": "compacted", "timestamp": "2026-10-15T00:00:03Z",
         "payload": {"window_id": "window-2", "message": "summary"}},
    ])
    def response(identifier: str, value: str) -> dict:
        return {"output": [{"type": "message", "role": "assistant", "id": identifier,
                            "content": [{"type": "output_text", "text": value}]}]}
    before = {"instructions": "system", "tools": [],
              "input": [{"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "task"}]}]}
    compact_request = {**before, "input": [*before["input"],
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "CONTEXT CHECKPOINT COMPACTION"}]}]}
    after = {**before, "input": [*before["input"],
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "summary"}]}]}
    manifest = _capture(tmp_path, root, [
        _call(1, root, "openai-responses", before, response("answer-1", "ack")),
        _call(2, root, "openai-responses", compact_request, None, status="unavailable"),
        _call(3, root, "openai-responses", compact_request, response("summary-msg", "summary")),
        _call(4, root, "openai-responses", after, response("answer-2", "done")),
    ])
    with pytest.raises(SFTConversionError, match="one-to-one"):
        build_compaction_segments(tmp_path, "codex", manifest)


def test_codex_unattributed_summary_does_not_require_text_matching(tmp_path: Path) -> None:
    _actf(tmp_path, "ACK", "DONE")
    _write(tmp_path / "trace.jsonl", [{"type": "compacted",
        "timestamp": "2026-10-15T00:00:03Z",
        "payload": {"window_id": "window-2", "message": "summary"}}])
    def call(index: int, owner: str | None, answer: str, status: str = "available") -> dict:
        return _call(index, owner, "openai-responses", {"instructions": "s", "tools": [],
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "summary" if index > 1 else "task"}]}]},
            {"output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": answer}]}]}, status=status)
    manifest = _capture(tmp_path, "root", [
        call(1, "root", "ACK"), call(2, None, "summary", "unavailable"),
        call(4, "root", "DONE")])
    segments = build_compaction_segments(tmp_path, "codex", manifest)
    assert len(segments) == 2
    assert segments[1][0]["summary_call_id"] is None
    assert segments[1][1]["messages"][-1]["content"] == "DONE"


def test_codex_text_and_tool_items_are_one_assistant_target(tmp_path: Path) -> None:
    _actf(tmp_path, "ACK", {"content": "Reading now", "tool_calls": [
        {"name": "read", "arguments": {"path": "a"}}]})
    _write(tmp_path / "trace.jsonl", [{"type": "compacted",
        "timestamp": "2026-10-15T00:00:03Z", "payload": {"window_id": "w"}}])
    def response(text: str, *, tool: bool = False) -> dict:
        blocks = ([{"type": "output_text", "text": "Reading "},
                   {"type": "output_text", "text": "now"}] if tool else
                  [{"type": "output_text", "text": text}])
        output = [{"type": "message", "role": "assistant", "content": blocks}]
        if tool:
            output.append({"type": "function_call", "call_id": "tool-1",
                           "name": "read", "arguments": '{"path":"a"}'})
        return {"output": output}
    def prompt(text: str) -> dict:
        return {"instructions": "s", "input": [{"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}]}], "tools": []}
    manifest = _capture(tmp_path, "root", [
        _call(1, "root", "openai-responses", prompt("task"), response("ACK")),
        _call(4, "root", "openai-responses", prompt("summary"),
              response("Reading now", tool=True)),
    ])
    segments = build_compaction_segments(tmp_path, "codex", manifest)
    assert len(segments) == 2
    target = segments[1][1]["messages"][-1]
    assert target["role"] == "assistant"
    assert target["content"] == "Reading now"
    assert len(target["tool_calls"]) == 1
    assert target["tool_calls"][0]["id"] == "tool-1"

    _actf(tmp_path, "ACK", {"content": "Reading now", "tool_calls": [
        {"name": "read", "arguments": {"path": "wrong"}}]})
    with pytest.raises(SFTConversionError, match="does not match"):
        build_compaction_segments(tmp_path, "codex", manifest)


@pytest.mark.parametrize("retained_reasoning", ["Inspect file.", ""])
def test_codex_segment_preserves_actf_reasoning_retention(
    tmp_path: Path, retained_reasoning: str,
) -> None:
    _actf(tmp_path, "ACK", {"content": "DONE", "reasoning_content": retained_reasoning,
                            "tool_calls": []})
    _write(tmp_path / "trace.jsonl", [{"type": "compacted",
        "timestamp": "2026-10-15T00:00:03Z", "payload": {"window_id": "w"}}])
    def call(index: int, prompt: str, answer: str, *, reasoning: bool = False) -> dict:
        output = ([{"type": "reasoning", "summary": [
            {"type": "summary_text", "text": "Inspect file."}]}] if reasoning else [])
        output.append({"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": answer}]})
        return _call(index, "root", "openai-responses", {"instructions": "s", "tools": [],
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": prompt}]}]}, {"output": output})
    manifest = _capture(tmp_path, "root", [
        call(1, "task", "ACK"), call(4, "summary", "DONE", reasoning=True)])
    segments = build_compaction_segments(tmp_path, "codex", manifest)
    assert segments[1][1]["messages"][-1]["reasoning_content"] == retained_reasoning


def test_incomplete_main_call_blocks_export(tmp_path: Path) -> None:
    root = "root:session"
    _actf(tmp_path, "answer")
    _write(tmp_path / "trace.jsonl", [{"type": "system", "subtype": "compact_boundary",
                                        "uuid": "b", "timestamp": "2026-10-15T00:00:02Z"}])
    manifest = _capture(tmp_path, root, [_call(3, root, "anthropic-messages",
        {"system": "s", "messages": [{"role": "user", "content": "summary"}], "tools": []},
        None, status="unavailable")])
    with pytest.raises(SFTConversionError, match="incomplete"):
        build_compaction_segments(tmp_path, "claude-code", manifest)


@pytest.mark.parametrize("root_ids", [[], ["root-a", "root-b"]])
def test_compaction_rejects_ambiguous_root_participants(
    tmp_path: Path, root_ids: list[str],
) -> None:
    _write(tmp_path / "trace.jsonl", [{"type": "system", "subtype": "compact_boundary",
                                        "uuid": "boundary", "timestamp": "2026-10-15T00:00:02Z"}])
    manifest = _capture(tmp_path, "root-a", [_call(3, "root-a", "anthropic-messages",
        {"system": "s", "messages": [{"role": "user", "content": "summary"}], "tools": []},
        {"content": [{"type": "text", "text": "answer"}]})])
    manifest_path = tmp_path / manifest
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["participants"] = [{"participant_id": root, "role": "root"} for root in root_ids]
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SFTConversionError, match="one observed root participant"):
        build_compaction_segments(tmp_path, "claude-code", manifest)


def test_unverified_assistant_in_later_request_is_not_trained(tmp_path: Path) -> None:
    root = "root:session"
    _actf(tmp_path, "one", "two")
    _write(tmp_path / "trace.jsonl", [{"type": "system", "subtype": "compact_boundary",
                                        "uuid": "b", "timestamp": "2026-10-15T00:00:03Z"}])
    first = {"system": "s", "tools": [],
             "messages": [{"role": "user", "content": "summary"}]}
    next_request = {**first, "messages": [*first["messages"],
        {"role": "assistant", "content": "one"},
        {"role": "assistant", "content": "injected without Call"},
        {"role": "user", "content": "continue"}]}
    manifest = _capture(tmp_path, root, [
        _call(4, root, "anthropic-messages", first,
              {"content": [{"type": "text", "text": "one"}]}),
        _call(5, root, "anthropic-messages", next_request,
              {"content": [{"type": "text", "text": "two"}]}),
    ])
    segments = build_compaction_segments(tmp_path, "claude-code", manifest)
    assert len(segments) == 2
    assert segments[1][1]["target_start_index"] == 5
    assert segments[1][1]["messages"][3]["content"] == "injected without Call"


@pytest.mark.parametrize("harness", ["openclaw", "opencode", "hermes", "deepseek", "pi"])
def test_chat_harness_context_rewrite_exports_actual_windows(tmp_path: Path, harness: str) -> None:
    _write(tmp_path / "trace.jsonl", [{"type": "session"}])
    (tmp_path / "trace_ACTF.json").write_text(json.dumps({"steps": [
        {"assistant_content": {"content": "", "tool_calls": [
            {"name": "read", "arguments": {"path": "a"}}]}},
        {"assistant_content": {"content": "intermediate", "tool_calls": []}},
        {"assistant_content": {"content": "final answer", "tool_calls": []}},
    ]}), encoding="utf-8")
    tool = {"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}
    first = {"messages": [{"role": "system", "content": "instructions"},
                          {"role": "user", "content": "read the file"}], "tools": [tool]}
    second = {"messages": [*first["messages"],
                           {"role": "assistant", "content": None, "tool_calls": [
                               {"id": "tool-1", "type": "function", "function": {
                                   "name": "read", "arguments": '{"path":"a"}'}}]},
                           {"role": "tool", "tool_call_id": "tool-1", "content": "old file"}],
              "tools": [tool]}
    resumed = {"messages": [{"role": "system", "content": "instructions"},
                            {"role": "user", "content": "compacted summary"},
                            {"role": "user", "content": "finish the task"}], "tools": [tool]}
    def answer(content: str, tool_calls: list[dict] | None = None) -> dict:
        return {"choices": [{"message": {"role": "assistant", "content": content,
                                        "tool_calls": tool_calls or []}}]}
    calls = [
        _call(1, "root", "openai-chat-completions", first, answer("", second["messages"][2]["tool_calls"])),
        _call(2, "root", "openai-chat-completions", second, answer("intermediate")),
        _call(3, "child", "openai-chat-completions", first, answer("child result")),
        _call(4, "root", "openai-chat-completions", resumed, answer("final answer")),
    ]
    manifest = _capture(tmp_path, "root", calls)
    segments = build_compaction_segments(tmp_path, harness, manifest)
    assert len(segments) == 2
    assert segments[0][1]["messages"][2]["tool_calls"][0]["id"] == "tool-1"
    assert segments[1][1]["target_start_index"] == 3
    assert [message["content"] for message in segments[1][1]["messages"]] == [
        "instructions", "compacted summary", "finish the task", "final answer"]
    assert segments[1][0]["boundary_event_id"] is None


def test_tool_schema_change_starts_a_new_window(tmp_path: Path) -> None:
    _write(tmp_path / "trace.jsonl", [{"type": "session"}])
    _actf(tmp_path, "ACK", "DONE")
    before = {"messages": [{"role": "user", "content": "task"}], "tools": []}
    after = {"messages": [*before["messages"],
                          {"role": "assistant", "content": "ACK"},
                          {"role": "user", "content": "continue"}],
             "tools": [{"type": "function", "function": {
                 "name": "read", "parameters": {"type": "object"}}}]}
    def output(answer: str) -> dict:
        return {"choices": [{"message": {"role": "assistant", "content": answer}}]}
    manifest = _capture(tmp_path, "root", [
        _call(1, "root", "openai-chat-completions", before, output("ACK")),
        _call(2, "root", "openai-chat-completions", after, output("DONE")),
    ])
    segments = build_compaction_segments(tmp_path, "hermes", manifest)
    assert len(segments) == 2
    assert segments[1][0]["boundary_source"] == "observed_context_change"
    assert segments[1][1]["tools"] == after["tools"]


@pytest.mark.parametrize("extra,following", [
    ("internal summary", "internal summary and next question"),
    ("a", "take next action"),
])
def test_chat_unbound_root_call_is_rejected_even_if_text_reappears(
    tmp_path: Path, extra: str, following: str,
) -> None:
    _write(tmp_path / "trace.jsonl", [{"type": "compaction"}])
    (tmp_path / "trace_ACTF.json").write_text(json.dumps({"steps": [
        {"assistant_content": {"content": "ACK", "tool_calls": []}},
        {"assistant_content": {"content": "final answer", "tool_calls": []}},
    ]}), encoding="utf-8")
    def call(index: int, prompt: str, output: str) -> dict:
        return _call(index, "root", "openai-chat-completions", {
            "messages": [{"role": "user", "content": prompt}], "tools": []},
            {"choices": [{"message": {"role": "assistant", "content": output}}]})
    calls = [
        call(1, "original task", "ACK"),
        call(2, "Summarize the original task", extra),
        call(3, following, "final answer"),
    ]
    manifest = _capture(tmp_path, "root", calls)
    with pytest.raises(SFTConversionError, match="one-to-one"):
        build_compaction_segments(tmp_path, "openclaw", manifest)


def test_openclaw_unbound_synthetic_reply_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path / "trace.jsonl", [
        {"type": "compaction", "id": "boundary", "timestamp": "2026-10-15T00:00:03Z",
         "summary": "saved summary"},
        {"type": "message", "parentId": "boundary", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "Context overflow"}]}},
    ])
    (tmp_path / "trace_ACTF.json").write_text(json.dumps({"steps": [
        {"assistant_content": {"content": "ACK", "tool_calls": []}},
        {"assistant_content": {"content": "Context overflow", "tool_calls": []}},
        {"assistant_content": {"content": "DONE", "tool_calls": []}},
    ]}), encoding="utf-8")
    def answer(identifier: str, content: str) -> dict:
        return {"id": identifier, "choices": [{"message": {
            "role": "assistant", "content": content}}]}
    calls = [
        _call(1, "root", "openai-chat-completions", {"messages": [
            {"role": "user", "content": "task"}], "tools": []}, answer("first", "ACK")),
        _call(2, None, "openai-chat-completions", {"messages": [
            {"role": "user", "content": "summarize"}], "tools": []},
              answer("summary", "saved summary"), status="unavailable"),
        _call(4, "root", "openai-chat-completions", {"messages": [
            {"role": "user", "content": "saved summary"},
            {"role": "assistant", "content": "ACKContext overflow"},
            {"role": "user", "content": "continue"}], "tools": []}, answer("last", "DONE")),
    ]
    calls[1].update(response_capture_status="complete", execution_status="completed")
    manifest = _capture(tmp_path, "root", calls)
    with pytest.raises(SFTConversionError, match="one-to-one"):
        build_compaction_segments(tmp_path, "openclaw", manifest)


def test_openclaw_unattributed_summary_without_synthetic_reply_exports(tmp_path: Path) -> None:
    _actf(tmp_path, "ACK", "DONE")
    _write(tmp_path / "trace.jsonl", [{"type": "compaction", "id": "boundary",
        "timestamp": "2026-10-15T00:00:03Z", "summary": "saved summary"}])
    def call(index: int, owner: str | None, prompt: str, answer: str,
             status: str = "available") -> dict:
        return _call(index, owner, "openai-chat-completions",
            {"messages": [{"role": "user", "content": prompt}], "tools": []},
            {"choices": [{"message": {"role": "assistant", "content": answer}}]},
            status=status)
    manifest = _capture(tmp_path, "root", [
        call(1, "root", "task", "ACK"),
        call(2, None, "summarize", "saved summary", "unavailable"),
        call(4, "root", "saved summary then continue", "DONE")])
    segments = build_compaction_segments(tmp_path, "openclaw", manifest)
    assert len(segments) == 2
    assert segments[1][0]["summary_call_id"] is None
    assert segments[1][0]["summary_call_binding"] == "unattributed"


def test_unmatched_main_reply_in_assistant_history_is_not_called_a_summary(tmp_path: Path) -> None:
    _write(tmp_path / "trace.jsonl", [{"type": "compaction"}])
    (tmp_path / "trace_ACTF.json").write_text(json.dumps({"steps": [
        {"assistant_content": {"content": "ACK", "tool_calls": []}},
        {"assistant_content": {"content": "DONE", "tool_calls": []}},
    ]}), encoding="utf-8")
    def call(index: int, messages: list[dict], answer: str) -> dict:
        return _call(index, "root", "openai-chat-completions",
                     {"messages": messages, "tools": []},
                     {"choices": [{"message": {"role": "assistant", "content": answer}}]})
    manifest = _capture(tmp_path, "root", [
        call(1, [{"role": "user", "content": "task"}], "ACK"),
        call(2, [{"role": "user", "content": "task"},
                 {"role": "assistant", "content": "ACK"}], "omitted main reply"),
        call(3, [{"role": "user", "content": "summary"},
                 {"role": "assistant", "content": "omitted main reply"},
                 {"role": "user", "content": "continue"}], "DONE"),
    ])
    with pytest.raises(SFTConversionError, match="one-to-one"):
        build_compaction_segments(tmp_path, "openclaw", manifest)


def test_unbound_auxiliary_call_cannot_be_ignored_without_a_native_boundary(tmp_path: Path) -> None:
    _write(tmp_path / "trace.jsonl", [{"type": "session"}])
    (tmp_path / "trace_ACTF.json").write_text(json.dumps({"steps": [
        {"assistant_content": {"content": "ACK", "tool_calls": []}},
        {"assistant_content": {"content": "DONE", "tool_calls": []}},
    ]}), encoding="utf-8")
    def call(index: int, messages: list[dict], answer: str) -> dict:
        return _call(index, "root", "openai-chat-completions",
                     {"messages": messages, "tools": []},
                     {"choices": [{"message": {"role": "assistant", "content": answer}}]})
    manifest = _capture(tmp_path, "root", [
        call(1, [{"role": "user", "content": "task"}], "ACK"),
        call(2, [{"role": "user", "content": "summarize task"}], "summary"),
        call(3, [{"role": "user", "content": "task"},
                 {"role": "assistant", "content": "ACK"},
                 {"role": "user", "content": "summary and continue"}], "DONE"),
    ])
    with pytest.raises(SFTConversionError, match="one-to-one"):
        build_compaction_segments(tmp_path, "hermes", manifest)


def test_chat_harness_keeps_linear_export_when_requests_only_append(tmp_path: Path) -> None:
    _write(tmp_path / "trace.jsonl", [{"type": "session"}])
    first = {"messages": [{"role": "system", "content": "instructions"},
                          {"role": "user", "content": "task"}], "tools": []}
    second = {"messages": [*first["messages"],
                           {"role": "assistant", "content": "answer"},
                           {"role": "user", "content": "continue"}], "tools": []}
    def answer(value: str) -> dict:
        return {"choices": [{"message": {"role": "assistant", "content": value}}]}
    manifest = _capture(tmp_path, "root", [
        _call(1, "root", "openai-chat-completions", first, answer("answer")),
        _call(2, "root", "openai-chat-completions", second, answer("done")),
    ])
    assert build_compaction_segments(tmp_path, "hermes", manifest) == []


def test_linear_image_and_mixed_native_shapes_do_not_trigger_compaction(tmp_path: Path) -> None:
    first = [{"role": "system", "content": "s"}, {"role": "user", "content": "look"}]
    second = [*first, {"role": "assistant", "content": "seen"},
              {"role": "user", "content": [{"type": "text", "text": "again"},
                                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,cG5n"}}]}]
    _write(tmp_path / "trace.jsonl", [
        {"type": "request", "body": {"messages": first}},
        {"type": "request", "body": {"messages": second}},
    ])
    manifest = _capture(tmp_path, "root", [
        _call(1, "root", "openai-chat-completions", {"messages": first, "tools": []},
              {"choices": [{"message": {"role": "assistant", "content": "seen"}}]}),
        _call(2, "root", "openai-chat-completions", {"messages": second, "tools": []},
              {"choices": [{"message": {"role": "assistant", "content": "done"}}]}),
    ])
    assert not has_main_compaction(tmp_path / "trace.jsonl", "hermes")
    assert build_compaction_segments(tmp_path, "hermes", manifest) == []


def test_anthropic_null_context_lists_raise_conversion_error() -> None:
    from roller.compaction_sft import _prompt_from_call
    for field in ("messages", "tools"):
        context = {"messages": [], "tools": []}
        context[field] = None
        with pytest.raises(SFTConversionError, match=field):
            _prompt_from_call({"protocol": "anthropic-messages", "context": context})


def test_collector_respects_explicit_sft_unavailability(tmp_path: Path) -> None:
    request = RunRequest(index=0, task=Task(id="unavailable", prompts=("task",), source_index=0),
                         model="model", timeout_seconds=30)
    collector = Collector(CollectionConfig(run_dir=tmp_path), lambda slot: None)
    sample = collector._sample_dir(request)
    sample.mkdir(parents=True)
    _write(sample / "trace.jsonl", [{"type": "session"}])
    (sample / "trace_ACTF.json").write_text(json.dumps({"schema_version": "ACTF_v1.0",
        "steps": [{"step_id": 1, "user_content": "task",
                   "assistant_content": {"content": "answer", "tool_calls": []}, "observation": []}]}),
        encoding="utf-8")
    result = collector._finalize_sample(request, sample, "hermes", HarnessResult(
        status="ok", elapsed_seconds=0.1, trace_path=str(sample / "trace.jsonl"),
        trajectory_path=str(sample / "trace_ACTF.json"), metadata={
            "trajectory_conversion": {"status": "unavailable", "target": "sft",
                                      "error": "source redacted"}}),
        [], "2026-10-15T00:00:00Z")
    assert result["runtime"]["trajectory_conversion"] == {
        "status": "unavailable", "target": "sft", "error": "source redacted"}
    assert result["trajectory"].get("sft_segments", []) == []
    assert not (sample / "trace_SFT.json").exists()


def test_missing_function_arguments_follow_linear_normalization() -> None:
    from roller.compaction_sft import _chat_message
    message = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call", "function": {"name": "read"}}]}
    assert _chat_message(message)["tool_calls"][0]["function"]["arguments"] == "{}"


def test_codex_opaque_reasoning_is_rejected_only_after_a_boundary(tmp_path: Path) -> None:
    _actf(tmp_path, "ACK", "DONE")
    first = _call(1, "root", "openai-responses", {"instructions": "s", "tools": [],
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "task"}]}]}, {"output": [
        {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "ACK"}]}]})
    second = _call(4, "root", "openai-responses", {"instructions": "s", "tools": [],
        "input": [{"type": "reasoning", "encrypted_content": "opaque"},
                  {"type": "message", "role": "user", "content": [
                      {"type": "input_text", "text": "continue"}]}]}, {"output": [
        {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "DONE"}]}]})
    summary = _call(2, None, "openai-responses", {"instructions": "s", "tools": [],
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "summarize"}]}]}, {"output": [
        {"type": "message", "role": "assistant", "id": "summary-id", "content": [
            {"type": "output_text", "text": "summary"}]}]}, status="unavailable")
    manifest = _capture(tmp_path, "root", [first, summary, second])
    _write(tmp_path / "trace.jsonl", [{"type": "session"}])
    assert build_compaction_segments(tmp_path, "codex", manifest) == []
    _write(tmp_path / "trace.jsonl", [
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                             "id": "summary-id", "content": [
                                                 {"type": "output_text", "text": "summary"}]}},
        {"type": "compacted", "timestamp": "2026-10-15T00:00:03Z",
         "payload": {"window_id": "w", "message": "summary"}},
    ])
    with pytest.raises(SFTConversionError, match="reasoning item cannot be represented"):
        build_compaction_segments(tmp_path, "codex", manifest)


def test_claude_root_attributed_summary_is_rejected_without_a_native_link(tmp_path: Path) -> None:
    _write(tmp_path / "trace.jsonl", [{"type": "system", "subtype": "compact_boundary",
                                        "uuid": "b", "timestamp": "2026-10-15T00:00:03Z"}])
    (tmp_path / "trace_ACTF.json").write_text(json.dumps({"steps": [
        {"assistant_content": {"content": "ACK", "tool_calls": []}},
        {"assistant_content": {"content": "DONE", "tool_calls": []}},
    ]}), encoding="utf-8")
    def call(index: int, prompt: str, answer: str) -> dict:
        return _call(index, "root", "anthropic-messages",
                     {"system": "s", "messages": [{"role": "user", "content": prompt}], "tools": []},
                     {"content": [{"type": "text", "text": answer}]})
    manifest = _capture(tmp_path, "root", [
        call(1, "task", "ACK"), call(2, "summarize task", "summary"),
        call(4, "summary then continue", "DONE")])
    with pytest.raises(SFTConversionError, match="one-to-one"):
        build_compaction_segments(tmp_path, "claude-code", manifest)


def test_native_window_splits_when_thinking_is_not_replayed(tmp_path: Path) -> None:
    _actf(tmp_path, "ACK", "NEXT", "DONE")
    _write(tmp_path / "trace.jsonl", [{"type": "system", "subtype": "compact_boundary",
                                        "uuid": "b", "timestamp": "2026-10-15T00:00:03Z"}])
    def call(index: int, messages: list[dict], output: list[dict]) -> dict:
        return _call(index, "root", "anthropic-messages",
                     {"system": "s", "messages": messages, "tools": []}, {"content": output})
    manifest = _capture(tmp_path, "root", [
        call(1, [{"role": "user", "content": "task"}], [{"type": "text", "text": "ACK"}]),
        call(4, [{"role": "user", "content": "summary"}],
             [{"type": "thinking", "thinking": "private"}, {"type": "text", "text": "NEXT"}]),
        call(5, [{"role": "user", "content": "summary"},
                 {"role": "assistant", "content": "NEXT"},
                 {"role": "user", "content": "finish"}], [{"type": "text", "text": "DONE"}]),
    ])
    segments = build_compaction_segments(tmp_path, "claude-code", manifest)
    assert [item[0]["boundary_source"] for item in segments] == [
        "initial", "native_boundary", "observed_context_change"]
    assert segments[2][1]["messages"][-1]["content"] == "DONE"


@pytest.mark.parametrize("harness", ["opencode", "hermes", "deepseek", "pi"])
def test_native_request_rewrite_blocks_linear_sft_without_capture(tmp_path: Path, harness: str) -> None:
    first = [{"role": "user", "content": "original task"}]
    resumed = [{"role": "user", "content": "compacted summary"}]
    _write(tmp_path / "trace.jsonl", [
        {"type": "request", "body": {"messages": first}, "messages": first},
        {"type": "request", "body": {"messages": resumed}, "messages": resumed},
    ])
    assert has_main_compaction(tmp_path / "trace.jsonl", harness)


def test_opencode_subagent_requests_do_not_look_like_main_compaction(tmp_path: Path) -> None:
    root = [{"role": "user", "content": "root task", "cache_control": {"type": "ephemeral"}}]
    _write(tmp_path / "trace.jsonl", [
        {"type": "session_export", "data": {"info": {"id": "root"}, "messages": []}},
        {"type": "session_export", "data": {"info": {"id": "child", "parentID": "root"}, "messages": []}},
        {"type": "request", "native_session_id": "root", "body": {"messages": root}},
        {"type": "request", "native_session_id": "child", "body": {
            "messages": [{"role": "user", "content": "child task"}]}},
        {"type": "request", "native_session_id": "root", "body": {
            "messages": [{"role": "user", "content": "root task"},
                         {"role": "assistant", "content": "tool call"}]}},
    ])
    assert not has_main_compaction(tmp_path / "trace.jsonl", "opencode")


def test_opencode_untagged_single_root_requests_keep_linear_sft(tmp_path: Path) -> None:
    first = [{"role": "user", "content": "read a file"}]
    rows = [
        {"type": "session_export", "data": {"info": {"id": "root"}, "messages": []}},
        {"type": "request", "body": {"messages": first}},
        {"type": "request", "body": {"messages": first + [
            {"role": "assistant", "content": "reading"},
            {"role": "tool", "content": "file contents"}]}},
    ]
    _write(tmp_path / "trace.jsonl", rows)
    assert not has_main_compaction(tmp_path / "trace.jsonl", "opencode")

    rows.append({"type": "session_export", "data": {
        "info": {"id": "child", "parentID": "root"}, "messages": []}})
    _write(tmp_path / "trace.jsonl", rows)
    assert has_main_compaction(tmp_path / "trace.jsonl", "opencode")


def test_opencode_child_request_can_precede_root_request(tmp_path: Path) -> None:
    _write(tmp_path / "trace.jsonl", [
        {"type": "session_export", "data": {"info": {"id": "root"}, "messages": []}},
        {"type": "session_export", "data": {"info": {"id": "child", "parentID": "root"}, "messages": []}},
        {"type": "request", "native_session_id": "child", "body": {"messages": [
            {"role": "user", "content": "child task"}]}},
        {"type": "request", "native_session_id": "root", "body": {"messages": [
            {"role": "user", "content": "old task"}]}},
        {"type": "request", "native_session_id": "root", "body": {"messages": [
            {"role": "user", "content": "summary"}]}},
    ])
    assert has_main_compaction(tmp_path / "trace.jsonl", "opencode")


def test_openclaw_native_compaction_blocks_linear_sft_without_capture(tmp_path: Path) -> None:
    _write(tmp_path / "trace.jsonl", [{"type": "compaction"}])
    assert has_main_compaction(tmp_path / "trace.jsonl", "openclaw")
    manifest = _capture(tmp_path, "root", [])
    with pytest.raises(SFTConversionError, match="lacks usable root Calls"):
        build_compaction_segments(tmp_path, "openclaw", manifest)


def test_pi_native_compaction_boundary_blocks_linear_sft(tmp_path: Path) -> None:
    _write(tmp_path / "trace.jsonl", [{"type": "event", "event": {"type": "compaction_end"}}])
    assert has_main_compaction(tmp_path / "trace.jsonl", "pi")


def test_collector_uses_chat_call_rewrite_instead_of_linear_actf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = RunRequest(index=0, task=Task(id="chat-reset", prompts=("task",), source_index=0),
                         model="model", timeout_seconds=30)
    collector = Collector(CollectionConfig(run_dir=tmp_path), lambda slot: None)
    sample = collector._sample_dir(request)
    sample.mkdir(parents=True)
    _write(sample / "trace.jsonl", [{"type": "session"}])
    (sample / "trace_ACTF.json").write_text(json.dumps({
        "schema_version": "ACTF_v1.0", "steps": [
            {"step_id": 1, "assistant_content": {"content": "old answer", "tool_calls": []}},
            {"step_id": 2, "assistant_content": {"content": "new answer", "tool_calls": []}},
        ]}), encoding="utf-8")
    def answer(value: str) -> dict:
        return {"choices": [{"message": {"role": "assistant", "content": value}}]}
    def context(value: str) -> dict:
        return {"messages": [{"role": "system", "content": "system"},
                             {"role": "user", "content": value}], "tools": []}
    calls = [
        _call(1, "root", "openai-chat-completions", context("task"), answer("old answer")),
        _call(2, "root", "openai-chat-completions", context("summary"), answer("new answer")),
    ]
    manifest = _capture(sample, "root", calls)
    result = collector._finalize_sample(
        request, sample, "hermes",
        HarnessResult(status="ok", elapsed_seconds=0.1,
                      trace_path=str(sample / "trace.jsonl"),
                      trajectory_path=str(sample / "trace_ACTF.json"),
                      metadata={"capture": {"manifest": manifest}}),
        [], "2026-10-15T00:00:00Z",
    )
    assert result["trajectory"]["sft"] is None
    assert len(result["trajectory"]["sft_segments"]) == 2
    assert not (sample / "trace_SFT.json").exists()
    monkeypatch.setattr("roller.collector.validate_capture", lambda *_: None)
    monkeypatch.setattr("roller.integrity.validate_capture", lambda *_: None)
    validate_resumable_sample(sample, request, result)
    collector._write_sft_dataset([result])
    assert (tmp_path / "sft.jsonl").read_text() == ""

    calls[1]["output"] = answer("unmatched output")
    _capture(sample, "root", calls)
    unavailable = collector._finalize_sample(
        request, sample, "hermes",
        HarnessResult(status="ok", elapsed_seconds=0.1,
                      trace_path=str(sample / "trace.jsonl"),
                      trajectory_path=str(sample / "trace_ACTF.json"),
                      metadata={"capture": {"manifest": manifest}}),
        [], "2026-10-15T00:00:00Z",
    )
    assert unavailable["runtime"]["trajectory_conversion"]["target"] == "sft"
    assert unavailable["trajectory"].get("sft_segments", []) == []
    validate_resumable_sample(sample, request, unavailable)


def test_collector_exports_aggregates_and_validates_compaction_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = RunRequest(index=0, task=Task(id="compact", prompts=("task",), source_index=0),
                         model="model", timeout_seconds=30)
    collector = Collector(CollectionConfig(run_dir=tmp_path), lambda slot: None)
    sample = collector._sample_dir(request)
    sample.mkdir(parents=True)
    _write(sample / "trace.jsonl", [{"type": "system", "subtype": "compact_boundary",
                                    "uuid": "boundary-1", "timestamp": "2026-10-15T00:00:03Z"}])
    (sample / "trace_ACTF.json").write_text(json.dumps({
        "schema_version": "ACTF_v1.0", "steps": [
            {"step_id": 1, "assistant_content": {"content": "ACK", "tool_calls": []}},
            {"step_id": 2, "assistant_content": {"content": "ANSWER", "tool_calls": []}}
        ]}), encoding="utf-8")
    before = {"system": "system", "tools": [],
              "messages": [{"role": "user", "content": "task"}]}
    after = {"system": "system", "tools": [],
             "messages": [{"role": "user", "content": "summary"},
                          {"role": "user", "content": "question"}]}
    manifest = _capture(sample, "root", [
        _call(1, "root", "anthropic-messages", before,
              {"content": [{"type": "text", "text": "ACK"}]}),
        _call(4, "root", "anthropic-messages", after,
              {"content": [{"type": "text", "text": "ANSWER"}]}),
    ])
    result = collector._finalize_sample(
        request, sample, "claude-code",
        HarnessResult(status="ok", elapsed_seconds=0.1,
                      trace_path=str(sample / "trace.jsonl"),
                      trajectory_path=str(sample / "trace_ACTF.json"),
                      metadata={"capture": {"manifest": manifest}}),
        [], "2026-10-15T00:00:00Z",
    )
    assert result["status"] == "ok"
    assert result["trajectory"]["sft"] is None
    assert len(result["trajectory"]["sft_segments"]) == 2
    assert sorted(path.name for path in (sample / "sft_segments").iterdir()) == ["0001.json", "0002.json"]
    assert "trajectory_conversion" not in result["runtime"]
    # Capture schema validation is tested separately; this fixture contains
    # only the Calls and root attribution needed for compaction export.
    monkeypatch.setattr("roller.collector.validate_capture", lambda *_: None)
    monkeypatch.setattr("roller.integrity.validate_capture", lambda *_: None)
    collector._write_sft_dataset([result])
    assert (tmp_path / "sft.jsonl").read_text() == ""

    validate_resumable_sample(sample, request, result)
    segment = sample / result["trajectory"]["sft_segments"][1]["path"]
    changed = json.loads(segment.read_text())
    changed["messages"][-1]["content"] = "wrong answer"
    segment.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match captured Call context"):
        validate_resumable_sample(sample, request, result)
