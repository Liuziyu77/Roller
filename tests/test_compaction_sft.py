"""Compaction windows must follow captured requests, including mid-turn resets."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from roller.compaction_sft import build_compaction_segments, export_compaction_segments
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


def test_claude_preserved_assistant_is_prompt_not_target(tmp_path: Path) -> None:
    root = "root:session"
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


def test_codex_summary_call_is_not_a_training_target(tmp_path: Path) -> None:
    root = "thread:root"
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
    segments = build_compaction_segments(tmp_path, "codex", manifest)
    assert len(segments) == 2
    assert segments[1][0]["boundary_event_id"] == "window-2"
    assert segments[1][0]["first_call_id"] == "call-4"
    assert segments[1][0]["summary_call_id"] == "call-3"
    assert segments[1][0]["summary_call_binding"] == "native_response_id"
    assert [m.get("content") for m in segments[1][1]["messages"] if m["role"] == "assistant"] == ["done"]


def test_incomplete_main_call_blocks_export(tmp_path: Path) -> None:
    root = "root:session"
    _write(tmp_path / "trace.jsonl", [{"type": "system", "subtype": "compact_boundary",
                                        "uuid": "b", "timestamp": "2026-10-15T00:00:02Z"}])
    manifest = _capture(tmp_path, root, [_call(3, root, "anthropic-messages",
        {"system": "s", "messages": [{"role": "user", "content": "summary"}], "tools": []},
        None, status="unavailable")])
    with pytest.raises(SFTConversionError, match="incomplete"):
        build_compaction_segments(tmp_path, "claude-code", manifest)


def test_unverified_assistant_in_later_request_is_not_trained(tmp_path: Path) -> None:
    root = "root:session"
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
    with pytest.raises(SFTConversionError, match="unverified assistant"):
        build_compaction_segments(tmp_path, "claude-code", manifest)


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
            {"step_id": 1, "assistant_content": {"content": "ANSWER", "tool_calls": []}}
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
                      metadata={"capture": {"manifest": manifest},
                                "trajectory_conversion": {"status": "unavailable", "target": "sft",
                                                          "error": "linear ACTF cannot express compaction"}}),
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
    assert len((tmp_path / "sft.jsonl").read_text().splitlines()) == 2

    validate_resumable_sample(sample, request, result)
    segment = sample / result["trajectory"]["sft_segments"][1]["path"]
    changed = json.loads(segment.read_text())
    changed["messages"][-1]["content"] = "wrong answer"
    segment.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match captured Call context"):
        validate_resumable_sample(sample, request, result)
