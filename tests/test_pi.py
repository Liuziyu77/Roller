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
from roller.harnesses.pi import PiConfig, PiHarness, RUNNER_PATH
from roller.pi_actf import convert_pi_trace, extract_pi_usage, pi_events_to_actf
from roller.models import RunRequest, Task
from roller.sft import actf_to_sft

TOOL = {"type": "function", "function": {"name": "bash", "description": "Run shell", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}


def native(message, turn=1):
    return {"type": "event", "turn": turn, "event": {"type": "message_end", "message": {"timestamp": 1000, **message}}}


def sample_events():
    request = {"type": "request", "turn": 1, "body": {"messages": [{"role": "system", "content": "Actual Pi system prompt"}], "tools": [deepcopy(TOOL)]}}
    return [
        {"type": "turn_start", "turn": 1},
        native({"role": "user", "content": "Read token"}),
        deepcopy(request),
        native({"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Read the file."},
            {"type": "toolCall", "id": "call-1", "name": "bash", "arguments": {"command": "cat token.txt"}}],
            "stopReason": "toolUse", "usage": {"input": 10, "output": 3, "cacheRead": 4}}),
        native({"role": "toolResult", "toolCallId": "call-1", "toolName": "bash", "isError": False,
                "content": [{"type": "text", "text": "TOKEN"}]}),
        deepcopy(request),
        native({"role": "assistant", "content": [{"type": "text", "text": "TOKEN"}],
                "stopReason": "stop", "usage": {"input": 20, "output": 2, "totalTokens": 22}}),
        {"type": "turn_end", "turn": 1},
    ]


def write_events(path, events=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in (sample_events() if events is None else events)), encoding="utf-8")


class PiTraceTests(unittest.TestCase):
    def test_native_tools_reasoning_and_cache_usage(self):
        actf = pi_events_to_actf(sample_events())
        self.assertEqual(actf["tools"], [TOOL])
        self.assertEqual(actf["system_prompt"], "Actual Pi system prompt")
        sft = actf_to_sft(actf)
        self.assertEqual([m["role"] for m in sft["messages"]], ["system", "user", "assistant", "tool", "assistant"])
        self.assertEqual(sft["messages"][3]["tool_call_id"], "call-1")
        self.assertEqual(sft["messages"][2]["reasoning_content"], "Read the file.")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "trace.jsonl"
            write_events(source)
            usage = extract_pi_usage(source)
            self.assertEqual((usage["request_count"], usage["input_tokens"], usage["output_tokens"], usage["cache_read_tokens"], usage["total_tokens"]), (2, 30, 5, 4, 39))

    def test_export_only_thinking_removal(self):
        events = sample_events()
        original = deepcopy(events)
        actf = pi_events_to_actf(events, keep_thinking=False)
        self.assertEqual(events, original)
        self.assertTrue(all(not step["assistant_content"]["reasoning_content"] for step in actf["steps"]))

    def test_multiturn_identical_replies_and_unicode_framing(self):
        events = sample_events()
        request = deepcopy(events[2]); request["turn"] = 2
        events += [{"type": "turn_start", "turn": 2},
            native({"role": "user", "content": "Again\u2028中文\u2029"}, 2), request,
            native({"role": "assistant", "content": [{"type": "text", "text": "TOKEN"}], "stopReason": "stop"}, 2),
            {"type": "turn_end", "turn": 2}]
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "trace.jsonl"
            write_events(source, events)
            sft = actf_to_sft(convert_pi_trace(source, Path(tmp) / "actf.json"))
            self.assertEqual([m["content"] for m in sft["messages"] if m["role"] == "user"], ["Read token", "Again\u2028中文\u2029"])
            self.assertEqual(sum(m.get("content") == "TOKEN" and m["role"] == "assistant" for m in sft["messages"]), 2)

    def test_image_tool_result_survives_actf_and_sft(self):
        events = sample_events()
        events[4]["event"]["message"]["content"].append({
            "type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png",
        })
        actf = pi_events_to_actf(events)
        image = actf["steps"][0]["observation"][0]["content"][1]
        self.assertEqual(image["type"], "image")
        sft = actf_to_sft(actf)
        self.assertEqual(sft["messages"][3]["content"], "TOKEN\n<image>")
        self.assertEqual(sft["images"], ["data:image/png;base64,iVBORw0KGgo="])

    def test_pure_and_multiple_image_tool_results_survive_in_order(self):
        image1 = {"type": "image", "data": "AA==", "mimeType": "image/png"}
        image2 = {"type": "image", "data": "AQ==", "mimeType": "image/jpeg"}
        for content, expected_text, expected_images in (
            ([image1], "<image>", ["data:image/png;base64,AA=="]),
            (
                [{"type": "text", "text": "two frames"}, image1, image2],
                "two frames\n<image>\n<image>",
                ["data:image/png;base64,AA==", "data:image/jpeg;base64,AQ=="],
            ),
        ):
            with self.subTest(expected_text=expected_text):
                events = sample_events()
                events[4]["event"]["message"]["content"] = deepcopy(content)
                sft = actf_to_sft(pi_events_to_actf(events))
                self.assertEqual(sft["messages"][3]["content"], expected_text)
                self.assertEqual(sft["images"], expected_images)

    def test_incomplete_or_lossy_data_rejected(self):
        for case in ("schema", "schema-change", "system-change", "arguments", "duplicate", "unpaired", "wrong-tool", "image", "runner", "length", "error", "no-end", "open-next-turn", "missing-request", "request-pending", "empty", "orphan", "compaction", "custom"):
            with self.subTest(case=case):
                events = sample_events()
                assistant = events[3]["event"]["message"]
                if case == "schema": events[2]["body"]["tools"] = []
                elif case == "schema-change": events[5]["body"]["tools"][0]["function"]["description"] = "changed"
                elif case == "system-change": events[5]["body"]["messages"][0]["content"] = "changed"
                elif case == "arguments": assistant["content"][1]["arguments"] = []
                elif case == "duplicate": assistant["content"].append(deepcopy(assistant["content"][1]))
                elif case == "unpaired": events.pop(4)
                elif case == "wrong-tool": events[4]["event"]["message"]["toolName"] = "write"
                elif case == "image": events[4]["event"]["message"]["content"].append({"type": "image", "data": "abc"})
                elif case == "runner": events.append({"type": "runner_error", "error": "failure"})
                elif case in {"length", "error"}: events[6]["event"]["message"]["stopReason"] = case
                elif case == "no-end": events.pop()
                elif case == "open-next-turn": events.append({"type": "turn_start", "turn": 2})
                elif case == "missing-request": events.pop(2)
                elif case == "request-pending": events.pop(6)
                elif case == "empty": events[6]["event"]["message"]["content"] = []
                elif case == "orphan": events.append(deepcopy(events[6]))
                elif case == "compaction": events.insert(2, {"type": "event", "turn": 1, "event": {"type": "compaction_start"}})
                else: events[6]["event"]["message"]["role"] = "custom"
                with self.assertRaises(ValueError): pi_events_to_actf(events)

    def test_corrupt_journal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "trace.jsonl"
            for content in ('{"type":', 'null\n'):
                source.write_text(content)
                with self.assertRaises(ValueError): convert_pi_trace(source, Path(tmp) / "actf.json")


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.result = {"ok": True, "content": "TOKEN"}
        self.process = Mock();self.process.wait.return_value = 0

    def run(self, args, timeout=None):
        self.calls.append(list(args));stdout = ""
        if args[:2] == ["image", "inspect"]:stdout = "sha256:test"
        elif args[-1] == "--check":stdout = "0.85.1"
        elif args[-1].endswith("/result.json"):stdout = json.dumps(self.result)
        elif args[0] == "cp" and args[1].endswith(":/roller/pi-state/trace.jsonl"):write_events(Path(args[2]))
        elif args[0] == "cp" and args[1].endswith("/outputs/."):(Path(args[2])/"answer.txt").write_text("TOKEN")
        return subprocess.CompletedProcess(args, 0, stdout, "")

    def popen(self, args, log_path, append=False):
        self.calls.append(list(args));return self.process

    def close_log(self, process):pass


class PiHarnessTests(unittest.TestCase):
    def test_sdk_runner_resolves_key_from_environment_and_enables_images(self):
        source = RUNNER_PATH.with_name("runner.mjs").read_text(encoding="utf-8")
        self.assertIn('apiKey: "$PI_API_KEY"', source)
        self.assertIn('input: ["text", "image"]', source)

    def test_session_outputs_cleanup_and_redaction(self):
        runtime = FakeRuntime()
        harness = PiHarness(PiConfig(api_key_env="LOCAL_TEST_KEY", wire_trace_mode="off"), 0, runtime)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LOCAL_TEST_KEY": "test-secret"}):
            result = harness.run(RunRequest(0, Task("test", ("one", "two"), 0), "model", 60), Path(tmp), 1)
            self.assertEqual(result.status, "ok", result.error)
            self.assertEqual(result.metadata["turn_count"], 2)
            self.assertEqual((Path(tmp)/"results/answer.txt").read_text(), "TOKEN")
            self.assertFalse((Path(tmp)/".pi-turn.json").exists())
            self.assertTrue(any(a[:2] == ["rm", "-f"] for a in runtime.calls))
            runtime.result = {"ok": False, "error": "failed test-secret"}
            result = harness.run(RunRequest(1, Task("error", ("one",), 0), "model", 60), Path(tmp)/"error", 1)
            self.assertEqual(result.status, "error")
            self.assertNotIn("test-secret", result.error)

    def test_timeout_stops_container_and_recovers_outputs(self):
        runtime = FakeRuntime();runtime.process.wait.side_effect = [subprocess.TimeoutExpired("run", 1), 0]
        harness = PiHarness(PiConfig(api_key_env="LOCAL_TEST_KEY", wire_trace_mode="off"), 0, runtime)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LOCAL_TEST_KEY": "test-secret"}):
            result = harness.run(RunRequest(0, Task("test", ("one",), 0), "model", 60), Path(tmp), 1)
            self.assertEqual(result.status, "timeout")
            self.assertTrue(any(a[0] == "kill" for a in runtime.calls))
            self.assertTrue((Path(tmp)/"results/answer.txt").is_file())

    def test_setup_step_timeout_is_an_error_not_a_task_timeout(self):
        class Runtime(FakeRuntime):
            def run(self, args, timeout=None):
                if args[0] == "run":
                    raise subprocess.TimeoutExpired(["docker", *args], timeout)
                return super().run(args, timeout)

        for budget, status, error in (
            (600, "error", "Pi container startup timed out after 60s"),
            (30, "timeout", "task exceeded 30s"),
        ):
            harness = PiHarness(PiConfig(api_key_env="LOCAL_TEST_KEY", wire_trace_mode="off"), 0, Runtime())
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LOCAL_TEST_KEY": "test-secret"}):
                result = harness.run(RunRequest(0, Task("test", ("one",), 0), "model", budget), Path(tmp), 1)
            self.assertEqual((result.status, result.error), (status, error))

    def test_conversion_failure_keeps_raw_and_recovers_outputs(self):
        runtime = FakeRuntime()
        harness = PiHarness(PiConfig(api_key_env="LOCAL_TEST_KEY", wire_trace_mode="off"), 0, runtime)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LOCAL_TEST_KEY": "test-secret"}), patch("roller.harnesses.pi.convert_pi_trace", side_effect=ValueError("invalid journal")):
            result = harness.run(RunRequest(0, Task("test", ("one",), 0), "model", 60), Path(tmp), 1)
            self.assertEqual(result.status, "error")
            self.assertIn("invalid journal", result.error)
            self.assertIsNone(result.trajectory_path)
            self.assertTrue((Path(tmp)/result.trace_path).is_file())
            self.assertEqual((Path(tmp)/"results/answer.txt").read_text(), "TOKEN")
            self.assertTrue(any(a[:2] == ["rm", "-f"] for a in runtime.calls))

    def test_invalid_settings_and_mixed_dry_run(self):
        for settings in ({"max_steps": 0}, {"max_steps": True}, {"context_length": 100}, {"output_tokens": 128000}, {"api_base": "https://user:secret@example.org"}, {"reasoning_effort": "adaptive"}, {"wire_trace_mode": "full"}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):PiConfig(**settings)
        with tempfile.TemporaryDirectory() as tmp:
            tasks = Path(tmp)/"tasks.json"
            tasks.write_text(json.dumps([{"id": "one", "harness": "pi", "prompt": "hi", "model": "model"}, {"id": "two", "harness": "hermes", "user_simulator": {"harness": "pi", "instruction": "finish", "max_turns": 1}, "model": "model"}]))
            args = ["collect", "--tasks", str(tasks), "--dry-run"]
            with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(_collect(_build_parser().parse_args(args)), 0)


if __name__ == "__main__":
    unittest.main()
