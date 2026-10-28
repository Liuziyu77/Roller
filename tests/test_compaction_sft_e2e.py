"""Exercise the complete local capture-to-SFT pipeline across harness protocols."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from roller.capture import validate_capture
from roller.collector import CollectionConfig, Collector
from roller.integrity import validate_resumable_sample
from roller.models import HarnessResult, RunRequest, Task
from roller.trajectory_audit import SEGMENTED_SFT_REASON, sft_exclusion_reason

HARNESSES = ("claude-code", "codex", "openclaw", "opencode", "hermes", "deepseek", "pi")


def _message(harness: str, role: str, content: str) -> dict:
    if harness == "codex":
        kind = "output_text" if role == "assistant" else "input_text"
        return {"type": "message", "role": role, "content": [{"type": kind, "text": content}]}
    return {"role": role, "content": content}


def _call(harness: str, seq: int, prompt: list[dict], answer: str,
          *, response_id: str | None = None) -> dict:
    if harness == "claude-code":
        protocol = "anthropic-messages"
        context = {"system": "instructions", "messages": prompt, "tools": []}
        output = {"content": [{"type": "text", "text": answer}]}
    elif harness == "codex":
        protocol = "openai-responses"
        context = {"instructions": "instructions", "input": prompt, "tools": []}
        output = {"output": [{**_message(harness, "assistant", answer),
                              "id": response_id or f"answer-{seq}"}]}
    else:
        protocol = "openai-chat-completions"
        context = {"messages": [{"role": "system", "content": "instructions"}, *prompt],
                   "tools": []}
        output = {"choices": [{"message": _message(harness, "assistant", answer)}]}
    return {"schema_version": "roller.call/v1", "call_id": f"call-{seq}",
            "request_seq": seq, "participant_id": "root", "protocol": protocol,
            "context": context, "output": output, "output_source": "model",
            "started_at": f"2026-10-17T00:00:{seq:02d}Z",
            "capture_status": "complete", "response_capture_status": "complete",
            "execution_status": "completed", "identity_status": "observed",
            "export_status": "available", "response_evidence_retained": False,
            "errors": []}


def _write_capture(sample: Path, calls: list[dict]) -> dict:
    directory = sample / "capture/attempt-0001"
    directory.mkdir(parents=True)
    (directory / "calls.jsonl").write_text(
        "".join(json.dumps(call) + "\n" for call in calls), encoding="utf-8")
    (directory / "relations.jsonl").write_text("", encoding="utf-8")
    files = [{"path": name, "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest()}
             for name in ("calls.jsonl", "relations.jsonl")]
    manifest = {"schema_version": "roller.capture/v1", "capture_status": "complete",
                "storage_layout": "compact/v2", "debug_retention": "off", "raw_retained": False,
                "files": files, "call_count": len(calls), "relation_count": 0,
                "participants": [{"participant_id": "root", "role": "root",
                                  "capture_status": "complete",
                                  "call_ids": [call["call_id"] for call in calls]}],
                "lifecycle": {"status": "complete", "pending_participant_ids": []}}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return {"manifest": "capture/attempt-0001/manifest.json", "capture_status": "complete"}


@pytest.mark.parametrize("harness", HARNESSES)
def test_three_context_windows_export_aggregate_and_resume(tmp_path: Path, harness: str) -> None:
    """Two resets preserve recent answers as prompt, never as fresh targets."""
    request = RunRequest(index=0, task=Task(id="two-resets", prompts=("old task",),
                                            source_index=0), model="model", timeout_seconds=30)
    collector = Collector(CollectionConfig(run_dir=tmp_path), lambda slot: None)
    sample = collector._sample_dir(request)
    sample.mkdir(parents=True)
    prompts = [
        [_message(harness, "user", "old task")],
        [_message(harness, "user", "summary one"),
         _message(harness, "assistant", "ACK"),
         _message(harness, "user", "continue")],
        [_message(harness, "user", "summary two"),
         _message(harness, "assistant", "NEXT"),
         _message(harness, "user", "finish")],
    ]
    calls = [_call(harness, seq, prompt, answer)
             for seq, prompt, answer in zip((1, 4, 7), prompts, ("ACK", "NEXT", "DONE"))]
    native = [{"type": "session"}]
    if harness == "claude-code":
        native = [{"type": "system", "subtype": "compact_boundary", "uuid": f"boundary-{i}",
                   "timestamp": f"2026-10-17T00:00:{seq:02d}Z"}
                  for i, seq in enumerate((3, 6), start=1)]
    elif harness == "codex":
        native = []
        for i, seq in enumerate((3, 6), start=1):
            summary = f"summary {i}"
            calls.append(_call(harness, seq - 1, [_message(harness, "user", "summarize")],
                               summary, response_id=f"summary-{i}"))
            native.extend([{"type": "response_item", "payload": {
                **_message(harness, "assistant", summary), "id": f"summary-{i}"}},
                {"type": "compacted", "timestamp": f"2026-10-17T00:00:{seq:02d}Z",
                 "payload": {"window_id": f"window-{i}", "message": summary}}])
        calls.sort(key=lambda call: call["request_seq"])
    (sample / "trace.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in native), encoding="utf-8")
    (sample / "trace_ACTF.json").write_text(json.dumps({
        "schema_version": "ACTF_v1.0",
        "steps": [{"step_id": i, "assistant_content": {"content": answer, "tool_calls": []}}
                  for i, answer in enumerate(("ACK", "NEXT", "DONE"), start=1)],
    }), encoding="utf-8")
    capture = _write_capture(sample, calls)
    result = collector._finalize_sample(
        request, sample, harness,
        HarnessResult(status="ok", elapsed_seconds=0.1,
                      trace_path=str(sample / "trace.jsonl"),
                      trajectory_path=str(sample / "trace_ACTF.json"),
                      metadata={"capture": capture}), [], "2026-10-17T00:00:00Z")
    validate_capture(sample, capture)
    validate_resumable_sample(sample, request, result)
    collector._write_sft_dataset([result])
    assert (tmp_path / "sft.jsonl").read_text() == ""
    linear_dir = tmp_path / "samples/linear/00000001_linear"
    linear_dir.mkdir(parents=True)
    linear_record = {"messages": [{"role": "user", "content": "next task"},
                                  {"role": "assistant", "content": "linear answer"}],
                     "tools": []}
    (linear_dir / "trace_SFT.json").write_text(json.dumps(linear_record), encoding="utf-8")
    collector._write_sft_dataset([result, {"status": "ok", "model": "linear",
                                           "source_index": 1, "task_id": "linear",
                                           "trajectory": {"sft": "trace_SFT.json"}}])
    assert [json.loads(row) for row in (tmp_path / "sft.jsonl").read_text().splitlines()] == [linear_record]
    records = [json.loads((sample / item["path"]).read_text())
               for item in result["trajectory"]["sft_segments"]]
    assert result["status"] == "ok"
    assert result["trajectory"]["sft"] is None
    assert sft_exclusion_reason(result) == SEGMENTED_SFT_REASON
    assert len(result["trajectory"]["sft_segments"]) == len(records) == 3
    assert [record["messages"][-1]["content"] for record in records] == ["ACK", "NEXT", "DONE"]
    assert [record["target_start_index"] for record in records] == [2, 4, 4]
    assert "old task" not in json.dumps(records[1:])
    assert "ACK" in json.dumps(records[1]) and "NEXT" in json.dumps(records[2])
    segment = sample / result["trajectory"]["sft_segments"][1]["path"]
    original = segment.read_text()
    corrupted = json.loads(original)
    corrupted["messages"][-1]["content"] = "corrupted target"
    segment.write_text(json.dumps(corrupted), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match captured Call context"):
        validate_resumable_sample(sample, request, result)
    segment.write_text(original, encoding="utf-8")
    validate_resumable_sample(sample, request, result)


@pytest.mark.parametrize("harness", HARNESSES)
def test_single_user_tool_round_trip_across_compaction(tmp_path: Path, harness: str) -> None:
    """A pending tool result stays in the resumed prompt within one user turn."""
    request = RunRequest(index=0, task=Task(id="tool-reset", prompts=("read a",),
                                            source_index=0), model="model", timeout_seconds=30)
    collector = Collector(CollectionConfig(run_dir=tmp_path), lambda slot: None)
    sample = collector._sample_dir(request)
    sample.mkdir(parents=True)
    call = _call(harness, 1, [_message(harness, "user", "read a")], "")
    resumed = _call(harness, 4, [], "DONE")
    if harness == "claude-code":
        tool = {"name": "read", "input_schema": {"type": "object"}}
        use = {"type": "tool_use", "id": "tool-1", "name": "read", "input": {"path": "a"}}
        call["output"] = {"content": [use]}
        resumed["context"]["messages"] = [
            _message(harness, "user", "summary"),
            {"role": "assistant", "content": [use]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1",
                                          "content": "file contents"}]},
        ]
        native = [{"type": "system", "subtype": "compact_boundary", "uuid": "boundary",
                   "timestamp": "2026-10-17T00:00:03Z"}]
    elif harness == "codex":
        tool = {"type": "function", "name": "read", "parameters": {"type": "object"}}
        use = {"type": "function_call", "call_id": "tool-1", "name": "read",
               "arguments": '{"path":"a"}'}
        call["output"] = {"output": [use]}
        resumed["context"]["input"] = [
            _message(harness, "user", "summary"), use,
            {"type": "function_call_output", "call_id": "tool-1", "output": "file contents"},
        ]
        native = [{"type": "response_item", "payload": {
            **_message(harness, "assistant", "summary"), "id": "summary-1"}},
            {"type": "compacted", "timestamp": "2026-10-17T00:00:03Z",
             "payload": {"window_id": "window-1", "message": "summary"}}]
    else:
        tool = {"type": "function", "function": {"name": "read",
                "parameters": {"type": "object"}}}
        use = {"id": "tool-1", "type": "function", "function": {
            "name": "read", "arguments": '{"path":"a"}'}}
        call["output"] = {"choices": [{"message": {"role": "assistant",
                            "content": None, "tool_calls": [use]}}]}
        resumed["context"]["messages"] = [
            {"role": "system", "content": "instructions"},
            _message(harness, "user", "summary"),
            {"role": "assistant", "content": None, "tool_calls": [use]},
            {"role": "tool", "tool_call_id": "tool-1", "content": "file contents"},
        ]
        native = ([{"type": "compaction"}] if harness == "openclaw" else
                  [{"type": "event", "event": {"type": "compaction_end"}}]
                  if harness == "pi" else [{"type": "session"}])
    call["context"]["tools"] = [tool]
    resumed["context"]["tools"] = [tool]
    calls = [call, resumed]
    if harness == "codex":
        calls.insert(1, _call(harness, 2, [_message(harness, "user", "summarize")],
                              "summary", response_id="summary-1"))
    (sample / "trace.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in native), encoding="utf-8")
    (sample / "trace_ACTF.json").write_text(json.dumps({
        "schema_version": "ACTF_v1.0", "steps": [
            {"step_id": 1, "assistant_content": {"content": "", "tool_calls": [
                {"name": "read", "arguments": {"path": "a"}}]}},
            {"step_id": 2, "assistant_content": {"content": "DONE", "tool_calls": []}},
        ]}), encoding="utf-8")
    capture = _write_capture(sample, calls)
    result = collector._finalize_sample(
        request, sample, harness,
        HarnessResult(status="ok", elapsed_seconds=0.1,
                      trace_path=str(sample / "trace.jsonl"),
                      trajectory_path=str(sample / "trace_ACTF.json"),
                      metadata={"capture": capture}), [], "2026-10-17T00:00:00Z")
    validate_capture(sample, capture)
    validate_resumable_sample(sample, request, result)
    collector._write_sft_dataset([result])
    assert (tmp_path / "sft.jsonl").read_text() == ""
    rows = [json.loads((sample / item["path"]).read_text())
            for item in result["trajectory"]["sft_segments"]]
    assert result["status"] == "ok"
    assert len(rows) == 2
    assert [message["role"] for message in rows[1]["messages"]] == [
        "system", "user", "assistant", "tool", "assistant"]
    assert rows[1]["target_start_index"] == 4
    assert rows[1]["messages"][2]["tool_calls"][0]["id"] == "tool-1"
    assert rows[1]["messages"][3]["tool_call_id"] == "tool-1"
    assert "read a" not in json.dumps(rows[1])


def test_codex_text_and_tool_items_export_through_collector(tmp_path: Path) -> None:
    request = RunRequest(index=0, task=Task(id="mixed-output", prompts=("task",),
                                           source_index=0), model="model", timeout_seconds=30)
    collector = Collector(CollectionConfig(run_dir=tmp_path), lambda slot: None)
    sample = collector._sample_dir(request)
    sample.mkdir(parents=True)
    (sample / "trace.jsonl").write_text(json.dumps({
        "type": "compacted", "timestamp": "2026-10-17T00:00:03Z",
        "payload": {"window_id": "window-1"}}) + "\n", encoding="utf-8")
    (sample / "trace_ACTF.json").write_text(json.dumps({
        "schema_version": "ACTF_v1.0", "steps": [
            {"step_id": 1, "assistant_content": {"content": "ACK", "tool_calls": []}},
            {"step_id": 2, "assistant_content": {"content": "Reading now", "tool_calls": [
                {"name": "read", "arguments": {"path": "a"}}]}},
        ]}), encoding="utf-8")
    calls = [_call("codex", 1, [_message("codex", "user", "task")], "ACK"),
             _call("codex", 4, [_message("codex", "user", "summary")], "Reading now")]
    calls[1]["output"]["output"].append({"type": "function_call", "call_id": "tool-1",
        "name": "read", "arguments": '{"path":"a"}'})
    capture = _write_capture(sample, calls)
    result = collector._finalize_sample(request, sample, "codex", HarnessResult(
        status="ok", elapsed_seconds=0.1, trace_path=str(sample / "trace.jsonl"),
        trajectory_path=str(sample / "trace_ACTF.json"), metadata={"capture": capture}),
        [], "2026-10-17T00:00:00Z")
    validate_capture(sample, capture)
    validate_resumable_sample(sample, request, result)
    records = [json.loads((sample / part["path"]).read_text())
               for part in result["trajectory"]["sft_segments"]]
    assert len(records) == 2
    assert records[1]["messages"][-1]["content"] == "Reading now"
    assert records[1]["messages"][-1]["tool_calls"][0]["id"] == "tool-1"


def test_linear_multimodal_sample_keeps_original_sft_export(tmp_path: Path) -> None:
    request = RunRequest(index=0, task=Task(id="image", prompts=("read",), source_index=0),
                         model="model", timeout_seconds=30)
    collector = Collector(CollectionConfig(run_dir=tmp_path), lambda slot: None)
    sample = collector._sample_dir(request)
    sample.mkdir(parents=True)
    first = [{"role": "system", "content": "s"}, {"role": "user", "content": "read"}]
    use = {"id": "tool-1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
    second = [*first, {"role": "assistant", "content": None, "tool_calls": [use]},
              {"role": "tool", "tool_call_id": "tool-1", "content": [
                  {"type": "image_url", "image_url": {"url": "data:image/png;base64,cG5n"}}]}]
    (sample / "trace.jsonl").write_text("".join(json.dumps({"type": "request", "body": {
        "messages": prompt}}) + "\n" for prompt in (first, second)), encoding="utf-8")
    (sample / "trace_ACTF.json").write_text(json.dumps({"schema_version": "ACTF_v1.0",
        "system_prompt": "s", "tools": [{"type": "function", "function": {"name": "read",
                                                     "parameters": {"type": "object"}}}],
        "steps": [{"step_id": 1, "user_content": "read", "assistant_content": {
            "content": "", "tool_calls": [{"id": "tool-1", "name": "read", "arguments": {}}]},
            "observation": [{"role": "toolResult", "toolCallId": "tool-1", "toolName": "read",
                             "content": [{"type": "image", "mimeType": "image/png", "data": "cG5n"}]}]},
            {"step_id": 2, "user_content": "", "assistant_content": {
                "content": "done", "tool_calls": []}, "observation": []}]}), encoding="utf-8")
    calls = [_call("hermes", 1, first[1:], "",), _call("hermes", 2, second[1:], "done")]
    calls[0]["context"]["messages"] = first
    calls[0]["output"] = {"choices": [{"message": {"role": "assistant", "content": None,
                                               "tool_calls": [use]}}]}
    calls[1]["context"]["messages"] = second
    capture = _write_capture(sample, calls)
    result = collector._finalize_sample(request, sample, "hermes", HarnessResult(
        status="ok", elapsed_seconds=0.1, trace_path=str(sample / "trace.jsonl"),
        trajectory_path=str(sample / "trace_ACTF.json"), metadata={"capture": capture}),
        [], "2026-10-17T00:00:00Z")
    validate_resumable_sample(sample, request, result)
    collector._write_sft_dataset([result])
    record = json.loads((sample / "trace_SFT.json").read_text())
    assert result["status"] == "ok" and "sft_segments" not in result["trajectory"]
    assert record["images"] == ["data:image/png;base64,cG5n"]
    assert len((tmp_path / "sft.jsonl").read_text().splitlines()) == 1


def test_terminal_compaction_keeps_resumable_linear_sft(tmp_path: Path) -> None:
    """OpenClaw can compact after its last Call; no prompt changed."""
    request = RunRequest(index=0, task=Task(id="terminal", prompts=("task",), source_index=0),
                         model="model", timeout_seconds=30)
    collector = Collector(CollectionConfig(run_dir=tmp_path), lambda slot: None)
    sample = collector._sample_dir(request)
    sample.mkdir(parents=True)
    (sample / "trace.jsonl").write_text(json.dumps({
        "type": "compaction", "id": "end", "summary": "",
        "timestamp": "2026-10-17T00:00:09Z"}) + "\n", encoding="utf-8")
    (sample / "trace_ACTF.json").write_text(json.dumps({
        "schema_version": "ACTF_v1.0", "system_prompt": "instructions", "tools": [],
        "steps": [{"step_id": 1, "user_content": "task", "assistant_content": {
            "content": "DONE", "tool_calls": []}, "observation": []}]}), encoding="utf-8")
    capture = _write_capture(sample, [
        _call("openclaw", 1, [_message("openclaw", "user", "task")], "DONE"),
        _call("openclaw", 2, [_message("openclaw", "user", "summarize")], "")])
    result = collector._finalize_sample(request, sample, "openclaw", HarnessResult(
        status="ok", elapsed_seconds=0.1, trace_path=str(sample / "trace.jsonl"),
        trajectory_path=str(sample / "trace_ACTF.json"), metadata={"capture": capture}),
        [], "2026-10-17T00:00:00Z")
    assert result["trajectory"]["sft"] == "trace_SFT.json"
    validate_resumable_sample(sample, request, result)


def test_raw_only_compaction_can_resume(tmp_path: Path) -> None:
    request = RunRequest(index=0, task=Task(id="raw-only", prompts=("task",), source_index=0),
                         model="model", timeout_seconds=30)
    collector = Collector(CollectionConfig(run_dir=tmp_path), lambda slot: None)
    sample = collector._sample_dir(request)
    sample.mkdir(parents=True)
    (sample / "trace.jsonl").write_text('{"type":"compaction"}\n', encoding="utf-8")
    info = {"schema_version": "roller.sample/v1", "status": "ok", "harness": "openclaw",
            "request_index": 0, "sample_index": 0, "sample_count": 1, "source_index": 0,
            "task_id": "raw-only", "model": "model",
            "trajectory": {"raw": "trace.jsonl", "actf": None, "sft": None},
            "runtime": {"trajectory_conversion": {"status": "unavailable", "target": "actf",
                                                   "error": "native trace incomplete"}}}
    validate_resumable_sample(sample, request, info)


@pytest.mark.parametrize("relative", ["../../../outside.json", "sft_segments/../../outside.json",
                                           "sft_segments/missing.json", "sft_segments/link.json", None])
def test_dataset_aggregation_rejects_unsafe_segment_path(tmp_path: Path, relative: str | None) -> None:
    request = RunRequest(index=0, task=Task(id="path", prompts=("task",), source_index=0),
                         model="model", timeout_seconds=30)
    collector = Collector(CollectionConfig(run_dir=tmp_path), lambda slot: None)
    sample = collector._sample_dir(request)
    (sample / "sft_segments").mkdir(parents=True)
    (tmp_path / "outside.json").write_text('{"messages":[],"tools":[]}', encoding="utf-8")
    (sample / "sft_segments/link.json").symlink_to(tmp_path / "outside.json")
    with pytest.raises(RuntimeError, match="SFT segment"):
        collector._write_sft_dataset([{"status": "ok", "path": sample.relative_to(tmp_path).as_posix(),
                                       "trajectory": {"sft_segments": [{"path": relative}]}}])
