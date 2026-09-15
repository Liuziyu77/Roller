from __future__ import annotations

import contextlib
from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from roller.cli import _build_parser, _collect
from roller.harnesses.opencode import OpenCodeConfig, OpenCodeHarness, RUNNER_PATH
from roller.opencode_actf import convert_opencode_trace, extract_opencode_usage, opencode_events_to_actf
from roller.models import RunRequest, Task
from roller.sft import actf_to_sft

TOOL = {"type": "function", "function": {"name": "bash", "description": "Run shell", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}


def sample_events():
    messages = [
        {"info": {"id": "m1", "role": "user", "sessionID": "s1", "time": {"created": 1000}}, "parts": [{"id": "p1", "messageID": "m1", "type": "text", "text": "Read token"}]},
        {"info": {"id": "m2", "role": "assistant", "sessionID": "s1", "parentID": "m1", "time": {"created": 1001, "completed": 1002}, "finish": "tool-calls", "tokens": {"input": 10, "output": 3, "cache": {"read": 4, "write": 0}}}, "parts": [
            {"id": "p2", "messageID": "m2", "type": "reasoning", "text": "Read the file."},
            {"id": "p3", "messageID": "m2", "type": "tool", "callID": "call-1", "tool": "bash", "state": {"status": "completed", "input": {"command": "cat token.txt"}, "output": "TOKEN", "time": {"start": 1001, "end": 1002}}},
        ]},
        {"info": {"id": "m3", "role": "assistant", "sessionID": "s1", "parentID": "m1", "time": {"created": 1003, "completed": 1004}, "finish": "stop", "tokens": {"input": 20, "output": 2, "total": 22}}, "parts": [{"id": "p4", "messageID": "m3", "type": "text", "text": "TOKEN"}]},
    ]
    return [{"type": "request", "body": {"messages": [{"role": "system", "content": "Actual OpenCode system prompt"}], "tools": [TOOL]}}, {"type": "session_export", "data": {"info": {"id": "s1"}, "messages": messages}}]


def write_events(path, events=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in (events or sample_events())))


class OpenCodeTraceTests(unittest.TestCase):
    def test_native_tools_reasoning_and_usage(self):
        events = sample_events()
        actf = opencode_events_to_actf(events)
        self.assertEqual(actf["tools"], [TOOL])
        self.assertEqual(actf["system_prompt"], "Actual OpenCode system prompt")
        sft = actf_to_sft(actf)
        self.assertEqual([m["role"] for m in sft["messages"]], ["system", "user", "assistant", "tool", "assistant"])
        self.assertEqual(sft["messages"][3]["tool_call_id"], "call-1")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            write_events(path, events + [events[-1]])
            usage = extract_opencode_usage(path)
            self.assertEqual((usage["request_count"], usage["input_tokens"], usage["output_tokens"], usage["cache_read_tokens"], usage["total_tokens"]), (2, 30, 5, 4, 39))

    def test_export_reasoning_does_not_change_native_history(self):
        events = sample_events()
        original = deepcopy(events)
        actf = opencode_events_to_actf(events, keep_thinking=False)
        self.assertEqual(events, original)
        self.assertTrue(all(not s["assistant_content"]["reasoning_content"] for s in actf["steps"]))

    def test_native_updates_and_exports_do_not_duplicate_messages(self):
        events = sample_events()
        messages = events[-1]["data"]["messages"]
        updates = []
        for m in messages:
            updates.append({"type": "event", "event": {"type": "message.updated", "properties": {"info": m["info"]}}})
            updates.extend({"type": "event", "event": {"type": "message.part.updated", "properties": {"part": p}}} for p in m["parts"])
        self.assertEqual(opencode_events_to_actf([events[0], *updates, events[-1], events[-1]]), opencode_events_to_actf(events))
        self.assertEqual(opencode_events_to_actf([events[0], *updates]), opencode_events_to_actf(events))

    def test_two_user_turns_keep_native_ids_and_identical_replies(self):
        events = sample_events()
        second = deepcopy(events[-1])
        rows = second["data"]["messages"]
        user, assistant = deepcopy(rows[0]), deepcopy(rows[-1])
        for row, mid, created in ((user, "m4", 1005), (assistant, "m5", 1006)):
            row["info"].update(id=mid, time={"created": created, "completed": created+1})
            for part in row["parts"]:part.update(id="p-"+mid, messageID=mid)
        assistant["info"]["parentID"] = "m4"
        rows.extend([user, assistant])
        sft = actf_to_sft(opencode_events_to_actf(events + [second]))
        self.assertEqual(sum(m["role"] == "user" for m in sft["messages"]), 2)
        self.assertEqual(sum(m["role"] == "assistant" and m.get("content") == "TOKEN" for m in sft["messages"]), 2)

    def test_unsafe_or_incomplete_native_data_is_rejected(self):
        for kind in ("schema", "changed_schema", "arguments", "duplicate", "pending", "attachment", "media", "error", "system"):
            with self.subTest(kind=kind):
                events = sample_events()
                rows = events[-1]["data"]["messages"]
                tool = rows[1]["parts"][1]
                if kind == "schema":events[0]["body"]["tools"] = []
                elif kind == "changed_schema":
                    changed = deepcopy(events[0]);changed["body"]["tools"][0]["function"]["description"] = "changed";events.insert(1, changed)
                elif kind == "arguments":tool["state"]["input"] = []
                elif kind == "duplicate":
                    other = deepcopy(tool);other["id"] = "p31";rows[1]["parts"].append(other)
                elif kind == "pending":tool["state"]["status"] = "pending"
                elif kind == "attachment":tool["state"]["attachments"] = [{"type": "file", "mime": "image/png"}]
                elif kind == "media":rows[0]["parts"][0]["type"] = "file"
                elif kind == "error":rows[1]["info"]["error"] = {"name": "APIError"}
                else:events[0]["body"]["messages"] = []
                with self.assertRaises(ValueError):opencode_events_to_actf(events)

    def test_corrupt_journal_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "trace.jsonl"
            source.write_text('{"type":')
            with self.assertRaises(ValueError):convert_opencode_trace(source, Path(tmp)/"trace_ACTF.json")

    def test_final_reply_checks_completion_and_current_user(self):
        spec = importlib.util.spec_from_file_location("opencode_runner_test", RUNNER_PATH)
        runner = importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
        export = sample_events()[-1]["data"]
        self.assertEqual(runner.native_reply(export), "TOKEN")
        for field, value in (("finish", "length"), ("parentID", "unknown"), ("error", {"name": "APIError"}), ("time", {"created": 1})):
            changed = deepcopy(export);changed["messages"][-1]["info"][field] = value
            with self.assertRaises(ValueError):runner.native_reply(changed)


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.result = {"ok": True, "content": "TOKEN"}
        self.process = Mock();self.process.wait.return_value = 0

    def run(self, args, timeout=None):
        self.calls.append(list(args));stdout = ""
        if args[:2] == ["image", "inspect"]:stdout = "sha256:test"
        elif args[-1] == "--check":stdout = "1.18.30"
        elif args[-1].endswith("/result.json"):stdout = json.dumps(self.result)
        elif args[0] == "cp" and args[1].endswith(":/roller/opencode-state/trace.jsonl"):write_events(Path(args[2]))
        elif args[0] == "cp" and args[1].endswith("/outputs/."):(Path(args[2])/"answer.txt").write_text("TOKEN")
        return subprocess.CompletedProcess(args, 0, stdout, "")

    def popen(self, args, log_path, append=False):
        self.calls.append(list(args));return self.process

    def close_log(self, process):pass


class OpenCodeHarnessTests(unittest.TestCase):
    def test_session_outputs_cleanup_and_redaction(self):
        runtime = FakeRuntime()
        harness = OpenCodeHarness(OpenCodeConfig(api_key_env="LOCAL_TEST_KEY", wire_trace_mode="off"), 0, runtime)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LOCAL_TEST_KEY": "test-secret"}):
            result = harness.run(RunRequest(0, Task("test", ("one", "two"), 0), "model", 60), Path(tmp), 1)
            self.assertEqual(result.status, "ok", result.error)
            self.assertEqual(result.metadata["turn_count"], 2)
            self.assertEqual((Path(tmp)/"results/answer.txt").read_text(), "TOKEN")
            self.assertFalse((Path(tmp)/".opencode-turn.json").exists())
            self.assertTrue(any(a[:2] == ["rm", "-f"] for a in runtime.calls))
            runtime.result = {"ok": False, "error": "failed test-secret"}
            result = harness.run(RunRequest(1, Task("error", ("one",), 0), "model", 60), Path(tmp)/"error", 1)
            self.assertEqual(result.status, "error")
            self.assertNotIn("test-secret", result.error)

    def test_timeout_stops_container_and_recovers_outputs(self):
        runtime = FakeRuntime();runtime.process.wait.side_effect = [subprocess.TimeoutExpired("run", 1), 0]
        harness = OpenCodeHarness(OpenCodeConfig(api_key_env="LOCAL_TEST_KEY", wire_trace_mode="off"), 0, runtime)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LOCAL_TEST_KEY": "test-secret"}):
            result = harness.run(RunRequest(0, Task("test", ("one",), 0), "model", 60), Path(tmp), 1)
            self.assertEqual(result.status, "timeout")
            self.assertTrue(any(a[0] == "kill" for a in runtime.calls))
            self.assertTrue((Path(tmp)/"results/answer.txt").is_file())

    def test_conversion_failure_keeps_raw_and_recovers_outputs(self):
        runtime = FakeRuntime()
        harness = OpenCodeHarness(OpenCodeConfig(api_key_env="LOCAL_TEST_KEY", wire_trace_mode="off"), 0, runtime)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LOCAL_TEST_KEY": "test-secret"}), patch("roller.harnesses.opencode.convert_opencode_trace", side_effect=ValueError("invalid journal")):
            result = harness.run(RunRequest(0, Task("test", ("one",), 0), "model", 60), Path(tmp), 1)
            self.assertEqual(result.status, "error")
            self.assertIn("invalid journal", result.error)
            self.assertIsNone(result.trajectory_path)
            self.assertTrue((Path(tmp)/result.trace_path).is_file())
            self.assertEqual((Path(tmp)/"results/answer.txt").read_text(), "TOKEN")
            self.assertTrue(any(a[:2] == ["rm", "-f"] for a in runtime.calls))

    def test_invalid_settings_and_mixed_dry_run(self):
        for settings in ({"max_steps": 0}, {"max_steps": True}, {"context_length": 100}, {"output_tokens": 128000}, {"api_base": "https://user:secret@example.org"}, {"reasoning_effort": "adaptive"}, {"wire_trace_mode": "full"}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):OpenCodeConfig(**settings)
        with tempfile.TemporaryDirectory() as tmp:
            tasks = Path(tmp)/"tasks.json"
            tasks.write_text(json.dumps([{"id": "one", "harness": "opencode", "prompt": "hi", "model": "model"}, {"id": "two", "harness": "hermes", "user_simulator": {"harness": "opencode", "instruction": "finish", "max_turns": 1}, "model": "model"}]))
            args = ["collect", "--tasks", str(tasks), "--dry-run"]
            with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(_collect(_build_parser().parse_args(args)), 0)


if __name__ == "__main__":
    unittest.main()
