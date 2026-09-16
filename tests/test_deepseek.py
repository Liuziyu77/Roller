from __future__ import annotations

import contextlib
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from roller.cli import _build_parser, _collect
from roller.harnesses.deepseek import DeepSeekConfig, DeepSeekHarness, RUNNER_PATH
from roller.deepseek_actf import convert_deepseek_trace, extract_deepseek_usage, deepseek_events_to_actf
from roller.models import RunRequest, Task
from roller.sft import actf_to_sft

TOOL = {"type": "function", "function": {"name": "bash", "description": "Run shell", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}


def native(kind, data, seq):
    event = {"type": kind, "data": data, "seq": seq, "time": 1000+seq}
    if kind in {"user/message", "assistant/message", "tool/result"}:
        event["surfaceOp"] = "append"
    return {"type": "notification", "method": "session.event", "payload": {"sessionId": "s1", "event": event}}


def sample_events():
    request = {"type": "request", "body": {"messages": [{"role": "system", "content": "Actual DeepSeek system prompt"}], "tools": [deepcopy(TOOL)]}}
    return [request,
        native("turn/start", {"turn": 1}, 0),
        native("user/message", {"role": "user", "content": [{"type": "text", "text": "Read token"}]}, 1),
        native("assistant/message", {"message": {"role": "assistant", "content": [
            {"type": "reasoning", "text": "Read the file."},
            {"type": "tool-call", "id": "call-1", "name": "bash", "arguments": '{"command":"cat token.txt"}'}]},
            "usage": {"inputTokens": 10, "outputTokens": 3, "cacheReadTokens": 4}}, 2),
        native("tool/result", {"message": {"role": "user", "content": [{"type": "tool-result", "toolCallId": "call-1", "content": [{"type": "text", "text": "TOKEN"}]}]}}, 3),
        deepcopy(request),
        native("assistant/message", {"message": {"role": "assistant", "content": [{"type": "text", "text": "TOKEN"}]}, "usage": {"inputTokens": 20, "outputTokens": 2, "totalTokens": 22}}, 4),
        native("turn/end", {"turn": 1, "reason": {"kind": "completed"}}, 5)]


def write_events(path, events=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e)+"\n" for e in (sample_events() if events is None else events)))


class DeepSeekTraceTests(unittest.TestCase):
    def test_native_tools_reasoning_and_usage(self):
        events = sample_events()
        actf = deepseek_events_to_actf(events)
        self.assertEqual(actf["tools"], [TOOL])
        self.assertEqual(actf["system_prompt"], "Actual DeepSeek system prompt")
        sft = actf_to_sft(actf)
        self.assertEqual([m["role"] for m in sft["messages"]], ["system", "user", "assistant", "tool", "assistant"])
        self.assertEqual(sft["messages"][3]["tool_call_id"], "call-1")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"trace.jsonl"
            write_events(path, events)
            usage = extract_deepseek_usage(path)
            self.assertEqual((usage["request_count"], usage["input_tokens"], usage["output_tokens"], usage["cache_read_tokens"], usage["total_tokens"]), (2, 30, 5, 4, 39))

    def test_export_reasoning_preserves_native_history(self):
        events = sample_events()
        original = deepcopy(events)
        actf = deepseek_events_to_actf(events, keep_thinking=False)
        self.assertEqual(events, original)
        self.assertTrue(all(not s["assistant_content"]["reasoning_content"] for s in actf["steps"]))

    def test_two_turns_preserve_identical_replies(self):
        events = sample_events()
        events += [native("turn/start", {"turn": 2}, 6),
            native("user/message", {"role": "user", "content": [{"type": "text", "text": "Again"}]}, 7),
            native("assistant/message", {"message": {"role": "assistant", "content": [{"type": "text", "text": "TOKEN"}]}}, 8),
            native("turn/end", {"turn": 2, "reason": {"kind": "completed"}}, 9)]
        sft = actf_to_sft(deepseek_events_to_actf(events))
        self.assertEqual(sum(m["role"] == "user" for m in sft["messages"]), 2)
        self.assertEqual(sum(m["role"] == "assistant" and m.get("content") == "TOKEN" for m in sft["messages"]), 2)

    def test_incomplete_or_lossy_data_rejected(self):
        for case in ("schema", "arguments", "duplicate", "unpaired", "image", "interrupted", "runner", "max-tokens", "error", "no-end", "open-next-turn", "replace", "system"):
            with self.subTest(case=case):
                events = sample_events()
                assistant = events[3]["payload"]["event"]["data"]
                if case == "schema":
                    for e in events:
                        if e["type"] == "request": e["body"]["tools"] = []
                elif case == "arguments": assistant["message"]["content"][1]["arguments"] = "[]"
                elif case == "duplicate": assistant["message"]["content"].append(deepcopy(assistant["message"]["content"][1]))
                elif case == "unpaired": events.pop(4)
                elif case == "image": assistant["message"]["content"].append({"type": "image"})
                elif case == "interrupted": assistant["interrupted"] = True
                elif case == "runner": events.append({"type": "runner_error", "error": "failure"})
                elif case in {"max-tokens", "error"}: events[-1]["payload"]["event"]["data"]["reason"]["kind"] = case
                elif case == "no-end": events.pop()
                elif case == "open-next-turn": events.append(native("turn/start", {"turn": 2}, 6))
                elif case == "replace": events[3]["payload"]["event"]["surfaceOp"] = {"op": "replace"}
                else:
                    for e in events:
                        if e["type"] == "request": e["body"]["messages"] = []
                with self.assertRaises(ValueError): deepseek_events_to_actf(events)

    def test_corrupt_journal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/"trace.jsonl"
            source.write_text('{"type":')
            with self.assertRaises(ValueError): convert_deepseek_trace(source, Path(tmp)/"trace_ACTF.json")


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.result = {"ok": True, "content": "TOKEN"}
        self.process = Mock();self.process.wait.return_value = 0

    def run(self, args, timeout=None):
        self.calls.append(list(args));stdout = ""
        if args[:2] == ["image", "inspect"]:stdout = "sha256:test"
        elif args[-1] == "--check":stdout = "0.1.5rc1"
        elif args[-1].endswith("/result.json"):stdout = json.dumps(self.result)
        elif args[0] == "cp" and args[1].endswith(":/roller/deepseek-state/trace.jsonl"):write_events(Path(args[2]))
        elif args[0] == "cp" and args[1].endswith("/outputs/."):(Path(args[2])/"answer.txt").write_text("TOKEN")
        return subprocess.CompletedProcess(args, 0, stdout, "")

    def popen(self, args, log_path, append=False):
        self.calls.append(list(args));return self.process

    def close_log(self, process):pass


class DeepSeekHarnessTests(unittest.TestCase):
    def test_session_outputs_cleanup_and_redaction(self):
        runtime = FakeRuntime()
        harness = DeepSeekHarness(DeepSeekConfig(api_key_env="LOCAL_TEST_KEY", wire_trace_mode="off"), 0, runtime)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LOCAL_TEST_KEY": "test-secret"}):
            result = harness.run(RunRequest(0, Task("test", ("one", "two"), 0), "model", 60), Path(tmp), 1)
            self.assertEqual(result.status, "ok", result.error)
            self.assertEqual(result.metadata["turn_count"], 2)
            self.assertEqual((Path(tmp)/"results/answer.txt").read_text(), "TOKEN")
            self.assertFalse((Path(tmp)/".deepseek-turn.json").exists())
            self.assertTrue(any(a[:2] == ["rm", "-f"] for a in runtime.calls))
            runtime.result = {"ok": False, "error": "failed test-secret"}
            result = harness.run(RunRequest(1, Task("error", ("one",), 0), "model", 60), Path(tmp)/"error", 1)
            self.assertEqual(result.status, "error")
            self.assertNotIn("test-secret", result.error)

    def test_timeout_stops_container_and_recovers_outputs(self):
        runtime = FakeRuntime();runtime.process.wait.side_effect = [subprocess.TimeoutExpired("run", 1), 0]
        harness = DeepSeekHarness(DeepSeekConfig(api_key_env="LOCAL_TEST_KEY", wire_trace_mode="off"), 0, runtime)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LOCAL_TEST_KEY": "test-secret"}):
            result = harness.run(RunRequest(0, Task("test", ("one",), 0), "model", 60), Path(tmp), 1)
            self.assertEqual(result.status, "timeout")
            self.assertTrue(any(a[0] == "kill" for a in runtime.calls))
            self.assertTrue((Path(tmp)/"results/answer.txt").is_file())

    def test_conversion_failure_keeps_raw_and_recovers_outputs(self):
        runtime = FakeRuntime()
        harness = DeepSeekHarness(DeepSeekConfig(api_key_env="LOCAL_TEST_KEY", wire_trace_mode="off"), 0, runtime)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LOCAL_TEST_KEY": "test-secret"}), patch("roller.harnesses.deepseek.convert_deepseek_trace", side_effect=ValueError("invalid journal")):
            result = harness.run(RunRequest(0, Task("test", ("one",), 0), "model", 60), Path(tmp), 1)
            self.assertEqual(result.status, "error")
            self.assertIn("invalid journal", result.error)
            self.assertIsNone(result.trajectory_path)
            self.assertTrue((Path(tmp)/result.trace_path).is_file())
            self.assertEqual((Path(tmp)/"results/answer.txt").read_text(), "TOKEN")
            self.assertTrue(any(a[:2] == ["rm", "-f"] for a in runtime.calls))

    def test_invalid_settings_and_mixed_dry_run(self):
        for settings in ({"max_steps": 0}, {"max_steps": True}, {"context_length": 100}, {"output_tokens": 128000}, {"api_base": "https://user:secret@example.org"}, {"reasoning_effort": "adaptive"}, {"wire_trace_mode": "full"}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):DeepSeekConfig(**settings)
        with tempfile.TemporaryDirectory() as tmp:
            tasks = Path(tmp)/"tasks.json"
            tasks.write_text(json.dumps([{"id": "one", "harness": "deepseek", "prompt": "hi", "model": "model"}, {"id": "two", "harness": "hermes", "user_simulator": {"harness": "deepseek", "instruction": "finish", "max_turns": 1}, "model": "model"}]))
            args = ["collect", "--tasks", str(tasks), "--dry-run"]
            with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(_collect(_build_parser().parse_args(args)), 0)


if __name__ == "__main__":
    unittest.main()
