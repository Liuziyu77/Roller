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
from roller.collector import CollectionConfig, Collector
from roller.harnesses.hermes import HermesConfig, HermesHarness, RUNNER_PATH
from roller.hermes_actf import convert_hermes_trace, extract_hermes_usage, hermes_events_to_actf
from roller.models import HarnessResult, RunRequest, Task
from roller.sft import actf_to_sft
from roller.trajectory_audit import sft_exclusion_reason


TOOL = {"type": "function", "function": {"name": "terminal", "description": "Run a command", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}


def event(role, content=None, **kwargs):
    return {"type": "message", "message": {"role": role, "content": content, **kwargs}}


def sample_events():
    return [
        event("user", "Read token"),
        {"type": "request", "messages": [{"role": "system", "content": "Actual Hermes prompt"}], "tools": [TOOL]},
        {**event("assistant", None, reasoning_content="Read the file.", tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "terminal", "arguments": '{"command":"cat token.txt"}'}}]), "usage": {"prompt_tokens": 10, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 4}}, "finish_reason": "tool_calls"},
        event("tool", "TOKEN", tool_call_id="call-1"),
        event("assistant", "TOKEN"),
    ]


def write_events(path, events=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in (events or sample_events())))


class HermesTraceTests(unittest.TestCase):
    def test_tool_schema_reasoning_usage_and_sft(self):
        trajectory = hermes_events_to_actf(sample_events())
        self.assertEqual(trajectory["system_prompt"], "Actual Hermes prompt")
        self.assertEqual(trajectory["tools"], [TOOL])
        self.assertEqual(trajectory["steps"][0]["metric"]["prompt_tokens_len"], 10)
        self.assertEqual(trajectory["steps"][0]["assistant_content"]["reasoning_content"], "Read the file.")
        sft = actf_to_sft(trajectory)
        self.assertEqual([m["role"] for m in sft["messages"]], ["system", "user", "assistant", "tool", "assistant"])
        self.assertEqual(sft["messages"][3]["tool_call_id"], "call-1")

    def test_export_can_drop_reasoning_without_mutating_raw(self):
        events = sample_events()
        original = deepcopy(events)
        result = hermes_events_to_actf(events, keep_thinking=False)
        self.assertEqual(result["steps"][0]["assistant_content"]["reasoning_content"], "")
        self.assertEqual(events, original)

    def test_compressed_request_does_not_erase_earlier_tools_or_duplicate_users(self):
        events = sample_events() + [event("user", "Again"), {"type": "request", "tools": [TOOL], "messages": [{"role": "system", "content": "Actual Hermes prompt"}, {"role": "user", "content": "Compressed summary"}]}, event("assistant", "TOKEN")]
        sft = actf_to_sft(hermes_events_to_actf(events))
        self.assertEqual([m["content"] for m in sft["messages"] if m["role"] == "user"], ["Read token", "Again"])
        self.assertEqual(len([m for m in sft["messages"] if m["role"] == "tool"]), 1)

    def test_parallel_tool_results_preserve_call_ids_and_order(self):
        events = sample_events()
        calls = events[2]["message"]["tool_calls"]
        calls.append({**deepcopy(calls[0]), "id": "call-2"})
        events.insert(3, event("tool", "SECOND", tool_call_id="call-2"))
        trajectory = hermes_events_to_actf(events)
        original = deepcopy(trajectory)
        sft = actf_to_sft(trajectory)
        results = [m for m in sft["messages"] if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in results], ["call-1", "call-2"])
        self.assertEqual([m["content"] for m in results], ["TOKEN", "SECOND"])
        self.assertEqual(trajectory, original)

    def test_invalid_native_tools_are_rejected(self):
        for mutation in ("missing_schema", "bad_json", "nonobject", "duplicate_call", "orphan_result"):
            with self.subTest(mutation=mutation):
                events = sample_events()
                if mutation == "missing_schema":
                    events[1]["tools"] = []
                elif mutation in {"bad_json", "nonobject"}:
                    events[2]["message"]["tool_calls"][0]["function"]["arguments"] = "{" if mutation == "bad_json" else "[]"
                elif mutation == "duplicate_call":
                    events.insert(4, deepcopy(events[2]))
                else:
                    events[3]["message"]["tool_call_id"] = "unknown"
                with self.assertRaises(ValueError):
                    hermes_events_to_actf(events)

    def test_media_is_not_silently_flattened(self):
        events = sample_events()
        events[0]["message"]["content"] = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
        with self.assertRaisesRegex(ValueError, "non-text"):
            hermes_events_to_actf(events)

    def test_preserves_image_tool_result_and_exports_multimodal_sft(self):
        events = sample_events()
        data_url = "data:image/png;base64,AA=="
        events[3]["message"]["content"] = [
            {"type": "text", "text": "image"},
            {"type": "image", "data": data_url},
        ]

        trajectory = hermes_events_to_actf(events)

        self.assertEqual(
            trajectory["steps"][0]["observation"][0]["content"][1],
            {"type": "image", "data": data_url},
        )
        sft = actf_to_sft(trajectory)
        tool_result = next(m for m in sft["messages"] if m["role"] == "tool")
        self.assertEqual(tool_result["content"], "image\n<image>")
        self.assertEqual(sft["images"], [data_url])

    def test_normalizes_openai_image_url_tool_result(self):
        events = sample_events()
        data_url = "data:image/png;base64,AA=="
        events[3]["message"]["content"] = [
            {"type": "text", "text": "image"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]

        trajectory = hermes_events_to_actf(events)

        self.assertEqual(
            trajectory["steps"][0]["observation"][0]["content"][1],
            {"type": "image", "data": data_url},
        )
        self.assertEqual(actf_to_sft(trajectory)["images"], [data_url])

    def test_corrupt_jsonl_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "trace.jsonl"
            write_events(source)
            with source.open("a") as stream:
                stream.write('{"type":')
            with self.assertRaises(ValueError):
                convert_hermes_trace(source, Path(tmp) / "actf.json")

    def test_unicode_line_separator_inside_json_string_is_preserved(self):
        events = sample_events()
        events[-1]["message"]["content"] = "TOKEN\u2028next"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "trace.jsonl"
            source.write_text(
                "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
                encoding="utf-8",
            )
            destination = Path(tmp) / "actf.json"
            actf = convert_hermes_trace(source, destination)
            self.assertEqual(actf_to_sft(actf)["messages"][-1]["content"], "TOKEN\u2028next")

    def test_usage_counts_each_response_once_and_handles_null_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "trace.jsonl"
            events = sample_events()
            events[-1]["usage"] = {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22, "prompt_tokens_details": None}
            events.append({"type": "response", "response": {"usage": events[-1]["usage"]}})
            write_events(source, events)
            usage = extract_hermes_usage(source)
            self.assertEqual((usage["request_count"], usage["input_tokens"], usage["output_tokens"], usage["total_tokens"], usage["cache_read_tokens"]), (2, 30, 5, 35, 4))


class HermesBridgeTests(unittest.TestCase):
    def test_actual_journal_request_controls_reach_collector_audit(self):
        spec = importlib.util.spec_from_file_location("roller_hermes_runner_test", RUNNER_PATH)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)

        class FakeAgent:
            def _interruptible_api_call(self, kwargs):
                self.forwarded = kwargs
                return {"choices": [{"message": {"role": "assistant", "content": "answer"},
                                     "finish_reason": "stop"}]}

        with tempfile.TemporaryDirectory() as temporary:
            sample_dir = Path(temporary) / "sample"
            sample_dir.mkdir()
            raw = sample_dir / "trace.jsonl"
            journal = runner.Journal(raw)
            journal.write("message", message={"role": "user", "content": "prompt"})
            agent = runner.journaled_agent(FakeAgent, journal)()
            agent._interruptible_api_call({
                "model": "model", "messages": [{"role": "user", "content": "prompt"}],
                "tools": [], "reasoning_effort": "high",
                "extra_body": {"reasoning": {"enabled": True, "effort": "high"},
                               "unrelated": "not needed for effort audit"},
            })
            request_row = next(row for row in (json.loads(line) for line in raw.read_text().splitlines())
                               if row["type"] == "request")
            self.assertEqual(agent.forwarded["reasoning_effort"], "high")
            self.assertEqual(request_row["reasoning_effort"], "high")
            self.assertEqual(request_row["extra_body"],
                             {"reasoning": {"enabled": True, "effort": "high"}})
            trajectory = sample_dir / "trace_ACTF.json"
            convert_hermes_trace(raw, trajectory)
            request = RunRequest(0, Task("task", ("prompt",), 0, thinking="low"), "model", 30)
            result = HarnessResult(
                status="ok", elapsed_seconds=0.01,
                trace_path="trace.jsonl", trajectory_path="trace_ACTF.json",
                metadata={"raw_trace_schema": "roller.hermes-events/v1",
                          "reasoning_effort": "low", "keep_thinking": True},
            )
            collector = Collector(CollectionConfig(run_dir=Path(temporary)), lambda slot: None)
            info = collector._finalize_sample(request, sample_dir, "hermes", result, [], "test")
            self.assertEqual(info["runtime"]["wire_effort_audit"]["source"], "native")
            self.assertEqual(info["runtime"]["wire_effort_audit"]["status"], "mismatch")
            self.assertEqual(sft_exclusion_reason(info), "wire_effort_mismatch")
            collector._write_sft_dataset([info])
            self.assertEqual((Path(temporary) / "sft.jsonl").read_text(), "")

    def test_tool_images_are_moved_to_following_user_message_on_wire(self):
        spec = importlib.util.spec_from_file_location("roller_hermes_runner_test", RUNNER_PATH)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        data_url = "data:image/png;base64,AA=="
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "call-1"}]},
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": [
                    {"type": "text", "text": "loaded"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]

        rewritten = runner.openai_compatible_tool_media(messages)

        self.assertEqual([message["role"] for message in rewritten], ["assistant", "tool", "user"])
        self.assertEqual(rewritten[1]["content"], "loaded")
        self.assertEqual(rewritten[2]["content"][1]["image_url"]["url"], data_url)
        self.assertIsInstance(messages[1]["content"], list)

    def test_runner_uses_native_toolsets_and_explicit_vision_opt_out(self):
        import sys
        from types import SimpleNamespace
        spec = importlib.util.spec_from_file_location("hermes_tool_selection", RUNNER_PATH)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        captured = []

        class NativeAgent:
            def __init__(self, **kwargs):
                captured.append(kwargs)

            def run_conversation(self, *args, **kwargs):
                return {"completed": True, "final_response": "OK", "messages": []}

        modules = {"run_agent": SimpleNamespace(AIAgent=NativeAgent),
                   "agent.auxiliary_client": SimpleNamespace(set_runtime_main=lambda *a, **k: None,
                                                             reset_runtime_main=lambda *a: None)}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, modules), patch.dict(
            os.environ, {"HERMES_HOME": tmp, "OPENAI_BASE_URL": "http://localhost/v1", "OPENAI_API_KEY": "test"}
        ):
            config = {"model": "test", "message": "hello", "session_id": "s", "max_iterations": 2,
                      "system_prompt": "test", "timeout_seconds": 10}
            runner.run_turn(config, tmp)
            self.assertIsNone(captured[-1]["enabled_toolsets"])
            self.assertIsNone(captured[-1]["disabled_toolsets"])
            runner.run_turn({**config, "toolsets": ["web", "vision"], "vision": False}, tmp)
            self.assertEqual(captured[-1]["enabled_toolsets"], ["web", "vision"])
            self.assertEqual(captured[-1]["disabled_toolsets"], ["vision"])

    def test_persistence_hook_keeps_tools_before_compaction_and_across_processes(self):
        spec = importlib.util.spec_from_file_location("roller_hermes_runner_test", RUNNER_PATH)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)

        class FakeAgent:
            def _interruptible_api_call(self, kwargs):
                return {"choices": [{"message": {"role": "assistant", "content": "same reply"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 7}}

            def _flush_messages_to_session_db(self, messages, conversation_history=None):
                messages.clear()  # Simulate compression after the hook.

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            journal = runner.Journal(path)
            agent = runner.journaled_agent(FakeAgent, journal)()
            messages = [{"role": "tool", "tool_call_id": "call-1", "content": "original", "_private": True}]
            agent._flush_messages_to_session_db(messages)
            self.assertEqual(messages, [])
            runner.Journal(path).observe_tools([{"role": "tool", "tool_call_id": "call-1", "content": "copy"}])
            for _ in range(2):
                agent._interruptible_api_call({"messages": [], "tools": [TOOL]})
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len([r for r in rows if r.get("message", {}).get("role") == "tool"]), 1)
            self.assertEqual(len([r for r in rows if r.get("message", {}).get("role") == "assistant"]), 2)
            self.assertNotIn("_private", rows[0]["message"])
            self.assertEqual(rows[1]["tools"], [TOOL])


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.turn_result = {"ok": True, "content": "TOKEN"}
        self.process = Mock()
        self.process.wait.return_value = 0

    def run(self, args, timeout=None):
        self.calls.append(list(args))
        stdout = ""
        if args[:2] == ["image", "inspect"]:
            stdout = "sha256:test"
        elif args[-1] == "--check":
            stdout = "Hermes pinned-test"
        elif args[-1].endswith("/result.json"):
            stdout = json.dumps(self.turn_result)
        elif args[0] == "cp" and args[1].endswith(":/roller/hermes-state/trace.jsonl"):
            write_events(Path(args[2]))
        elif args[0] == "cp" and args[1].endswith("/outputs/."):
            (Path(args[2]) / "answer.txt").write_text("TOKEN")
        return subprocess.CompletedProcess(args, 0, stdout, "")

    def popen(self, args, log_path, append=False):
        self.calls.append(list(args))
        return self.process

    def close_log(self, process):
        pass


class HermesSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = FakeRuntime()
        self.key = patch.dict("os.environ", {"ROLLER_TEST_KEY": "test-private-key"})
        self.key.start()
        self.addCleanup(self.key.stop)

    def session(self, **kwargs):
        harness = HermesHarness(HermesConfig(api_key_env="ROLLER_TEST_KEY", **kwargs), 0, self.runtime)
        return harness.create_session(RunRequest(0, Task("task", ("read",), 0, harness="hermes"), "test-model", 120), self.root, 1)

    def test_start_two_turns_collect_and_idempotent_finish(self):
        session = self.session()
        session.start()
        for text in ("first", "second"):
            self.assertEqual(session.send(text, 30).content, "TOKEN")
        result = session.finish("ok")
        self.assertEqual(result.status, "ok", result.error)
        self.assertEqual(result.metadata["turn_count"], 2)
        self.assertTrue(result.metadata["output_collection"]["passed"])
        self.assertEqual(result.metadata["provenance"]["harness"], "hermes")
        self.assertEqual(len([c for c in self.runtime.calls if c[0] == "run"]), 1)
        self.assertIs(session.finish("error"), result)
        self.assertEqual(len([c for c in self.runtime.calls if c[:2] == ["rm", "-f"]]), 1)
        self.assertNotIn("test-private-key", json.dumps(result.metadata))

    def test_native_failure_is_error_and_key_is_redacted(self):
        self.runtime.turn_result = {"ok": False, "error": "rejected test-private-key"}
        harness = HermesHarness(HermesConfig(api_key_env="ROLLER_TEST_KEY"), 0, self.runtime)
        result = harness.run(RunRequest(0, Task("task", ("read",), 0), "model", 120), self.root, 1)
        self.assertEqual(result.status, "error")
        self.assertNotIn("test-private-key", result.error)
        self.assertTrue(any(c[:2] == ["rm", "-f"] for c in self.runtime.calls))

    def test_timeout_kills_execution_and_collects_partial_journal(self):
        session = self.session()
        session.start()
        self.runtime.process.wait.side_effect = [subprocess.TimeoutExpired("docker", 1), 0]
        with self.assertRaises(TimeoutError):
            session.send("read", 1)
        self.runtime.process.kill.assert_called_once()
        result = session.finish("timeout", "deadline")
        self.assertEqual(result.status, "timeout")
        self.assertIsNotNone(result.trace_path)
        self.assertTrue(any(c[0] == "kill" for c in self.runtime.calls))

    def test_simulator_session_can_skip_output_collection(self):
        harness = HermesHarness(HermesConfig(api_key_env="ROLLER_TEST_KEY"), 0, self.runtime)
        session = harness.create_session(RunRequest(0, Task("sim", (), 0), "model", 120), self.root, 1, collect_outputs=False)
        session.start()
        self.assertEqual(session.finish("ok").status, "ok")
        self.assertFalse(any(c[0] == "cp" and c[1].endswith("/outputs/.") for c in self.runtime.calls))

    def test_conversion_failure_still_collects_outputs_and_removes_container(self):
        session = self.session()
        session.start()
        with patch("roller.harnesses.hermes.convert_hermes_trace", side_effect=ValueError("broken trace")):
            result = session.finish("ok")
        self.assertEqual(result.status, "error")
        self.assertIn("broken trace", result.error)
        self.assertTrue(result.metadata["output_collection"]["passed"])
        self.assertTrue(any(c[:2] == ["rm", "-f"] for c in self.runtime.calls))

    def test_failed_revision_check_cleans_up_allocated_container(self):
        session = self.session()
        original = self.runtime.run

        def run(args, timeout=None):
            if args[-1] == "--check":
                return subprocess.CompletedProcess(args, 1, "", "Hermes revision mismatch")
            return original(args, timeout)

        self.runtime.run = run
        with self.assertRaisesRegex(RuntimeError, "revision mismatch"):
            session.start()
        self.assertEqual(session.finish("error", "revision mismatch").status, "error")
        self.assertFalse(any("--config" in c for c in self.runtime.calls))
        self.assertTrue(any(c[:2] == ["rm", "-f"] for c in self.runtime.calls))

    def test_bad_configuration_rejected_before_container_start(self):
        for kwargs in ({"context_length": 32000}, {"context_length": True}, {"max_iterations": 0}, {"max_iterations": True}, {"reasoning_effort": "adaptive"}, {"api_base": "http://user:secret@example.test"}, {"api_base": "relative"}, {"wire_trace_mode": "full"}, {"api_key_env": "BAD-NAME"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                HermesConfig(**kwargs)


class HermesCliTests(unittest.TestCase):
    def test_openclaw_simulator_model_config_changes_invalidate_hermes_run_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks, config = root / "tasks.json", root / "models.json"
            tasks.write_text(json.dumps([{"harness": "hermes", "model": "hermes-model", "user_simulator": {"harness": "openclaw", "model": "provider/model", "instruction": "Ask OK"}}]))
            model_config = {"providers": {"provider": {"baseUrl": "https://first.example/v1", "apiKey": "${OC_TEST_KEY}", "api": "openai-completions", "models": [{"id": "model"}]}}}
            config.write_text(json.dumps(model_config))
            command = ["collect", "--tasks", str(tasks), "--model-config", str(config), "--output", str(root / "runs"), "--run-name", "mixed"]
            with patch.dict("os.environ", {"OC_TEST_KEY": "test-only-key"}), patch("roller.cli._docker_preflight", return_value="sha256:test"), patch("roller.cli.Collector") as collector, contextlib.redirect_stdout(io.StringIO()):
                collector.return_value.collect.return_value = []
                self.assertEqual(_collect(_build_parser().parse_args(command)), 0)
                model_config["providers"]["provider"]["baseUrl"] = "https://changed.example/v1"
                config.write_text(json.dumps(model_config))
                with self.assertRaisesRegex(Exception, "lock|configuration|mismatch"):
                    _collect(_build_parser().parse_args(command + ["--resume"]))
            info = json.loads((root / "runs/mixed/info.json").read_text())
            for name in ("hermes", "openclaw"):
                self.assertEqual(info["configuration"][name]["docker_image_digest"], "sha256:test")

    def test_cli_routes_real_adapter_config_and_locks_iteration_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "tasks.json"
            path.write_text(json.dumps([{"harness": "hermes", "model": "model", "prompt": "OK"}]))
            command = ["collect", "--tasks", str(path), "--output", str(root / "runs"), "--run-name", "case", "--api-base", "https://shared.example/v1", "--hermes-api-base", "https://hermes.example/v1", "--api-key-env", "SHARED_KEY", "--hermes-api-key-env", "HERMES_KEY", "--hermes-max-iterations", "5"]
            configurations = []

            def make(config, slot):
                configurations.append(config)
                return HermesHarness(config, slot, FakeRuntime())

            with patch.dict("os.environ", {"HERMES_KEY": "test-key"}), patch("roller.cli._docker_preflight", return_value="sha256:test"), patch("roller.cli.HermesHarness", side_effect=make), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(_collect(_build_parser().parse_args(command)), 0)
                self.assertEqual(configurations[0].api_base, "https://hermes.example/v1")
                self.assertEqual(configurations[0].api_key_env, "HERMES_KEY")
                self.assertEqual(configurations[0].max_iterations, 5)
                self.assertEqual(_collect(_build_parser().parse_args(command + ["--resume"])), 0)
                self.assertEqual(len(configurations), 1)
                with self.assertRaisesRegex(Exception, "lock|configuration|mismatch"):
                    _collect(_build_parser().parse_args(command + ["--resume", "--hermes-max-iterations", "6"]))
            info = json.loads((root / "runs/case/info.json").read_text())
            self.assertEqual(info["configuration"]["hermes"]["docker_image_digest"], "sha256:test")
            self.assertNotIn("test-key", (root / "runs/case/lock.json").read_text())

    def test_mixed_dry_run_accepts_hermes_on_both_sides_without_docker_or_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.json"
            path.write_text(json.dumps([
                {"harness": "hermes", "model": "model", "prompt": "OK"},
                {"harness": "codex", "model": "model", "user_simulator": {"harness": "hermes", "model": "model", "instruction": "Ask OK"}},
                {"harness": "hermes", "model": "model", "user_simulator": {"harness": "claude-code", "model": "model", "instruction": "Ask OK"}},
            ]))
            args = _build_parser().parse_args(["collect", "--tasks", str(path), "--hermes-max-iterations", "4", "--dry-run"])
            output = io.StringIO()
            with patch("roller.cli._docker_preflight", side_effect=AssertionError("Docker must not run")), contextlib.redirect_stdout(output):
                self.assertEqual(_collect(args), 0)
            self.assertEqual(json.loads(output.getvalue())["trajectories"], 3)


if __name__ == "__main__":
    unittest.main()
