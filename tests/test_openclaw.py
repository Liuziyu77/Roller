from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from roller.bundles import TaskBundleStore
from roller.harnesses.openclaw import (
    DEFAULT_OPENCLAW_IMAGE,
    OPENCLAW_STATE_CONTAINER_PATH,
    TRANSCRIPT_EXPORT_CONTAINER_PATH,
    OpenClawConfig,
    OpenClawHarness,
    expand_env_placeholders,
    load_model_config,
    prepare_model_config,
    validate_openclaw_thinking,
)
from roller.models import RunRequest, Task
from roller.tasks import load_tasks
from roller.usage import extract_openclaw_usage


class GatewayReadinessTests(unittest.TestCase):
    def test_slow_probe_retries_within_startup_budget(self):
        runtime = mock.Mock()
        runtime.run.side_effect = [subprocess.TimeoutExpired("docker exec", 2), subprocess.CompletedProcess([], 0)]
        process = mock.Mock()
        process.poll.return_value = None
        harness = OpenClawHarness(OpenClawConfig(gateway_startup_timeout=10), 0, runtime=runtime)
        with mock.patch("roller.harnesses.openclaw.time.monotonic", side_effect=[0, 0, 2]):
            harness._wait_for_gateway("container", process)
        self.assertEqual(runtime.run.call_count, 2)

    def test_probe_timeouts_still_respect_startup_deadline(self):
        runtime = mock.Mock()
        runtime.run.side_effect = subprocess.TimeoutExpired("docker exec", 2)
        process = mock.Mock()
        process.poll.return_value = None
        harness = OpenClawHarness(OpenClawConfig(gateway_startup_timeout=3), 0, runtime=runtime)
        with mock.patch("roller.harnesses.openclaw.time.monotonic", side_effect=[0, 0, 2, 4]):
            with self.assertRaisesRegex(TimeoutError, "gateway did not listen.*3s"):
                harness._wait_for_gateway("container", process)
        self.assertEqual(runtime.run.call_count, 2)


class FakeProcess:
    def __init__(self, gateway: bool = False) -> None:
        self.gateway = gateway
        self.running = True
        self.returncode = None

    def poll(self):
        return None if self.running else self.returncode

    def wait(self, timeout=None):
        if not self.gateway:
            self.running = False
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.running = False
        self.returncode = 0

    def kill(self):
        self.running = False
        self.returncode = -9


class FakeDockerRuntime:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.agent_messages: list[str] = []
        self.assistant_extractions = 0

    def run(self, args, timeout=None):
        args = list(args)
        self.commands.append(args)
        stdout = "2026.1.0\n" if args[-2:] == ["openclaw", "--version"] else ""
        if args and "ROLLER_SDK_LAST_ASSISTANT" in args[-1]:
            self.assistant_extractions += 1
            stdout = json.dumps(
                {
                    "content": "done",
                    "assistant_count": self.assistant_extractions,
                }
            ) + "\n"
        if (
            args
            and args[0] == "cp"
            and f":{TRANSCRIPT_EXPORT_CONTAINER_PATH}" in args[1]
        ):
            destination = Path(args[2])
            event = {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "api": "openai-completions",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {"input": 3, "output": 2, "totalTokens": 5},
                },
            }
            destination.write_text(json.dumps(event) + "\n", encoding="utf-8")
        if args and args[0] == "cp" and ":/tmp_workspace/deliverables/." in args[1]:
            destination = Path(args[2])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "report.md").write_text("generated", encoding="utf-8")
            (destination / "extra.log").write_text("also collected", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    def popen(self, args, log_path, append=False):
        args = list(args)
        self.commands.append(args)
        if "agent" in args:
            self.agent_messages.append(args[args.index("--message") + 1])
        return FakeProcess(gateway="gateway" in args)

    @staticmethod
    def close_log(process):
        return None


class FakeCacheTraceRuntime(FakeDockerRuntime):
    def run(self, args, timeout=None):
        result = super().run(args, timeout=timeout)
        args = list(args)
        if args and args[0] == "cp" and ":/tmp/roller-cache-trace.jsonl" in args[1]:
            Path(args[2]).write_text(
                json.dumps({"options": {"apiKey": "secret"}}) + "\n",
                encoding="utf-8",
            )
        return result


class CliDeliveryCopyRuntime(FakeDockerRuntime):
    """Transcripts that also record the CLI-delivered copy of each reply."""

    def run(self, args, timeout=None):
        result = super().run(args, timeout=timeout)
        args = list(args)
        if (
            args
            and args[0] == "cp"
            and f":{TRANSCRIPT_EXPORT_CONTAINER_PATH}" in args[1]
        ):
            with Path(args[2]).open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "type": "message",
                            "message": {
                                "role": "assistant",
                                "api": "cli",
                                "content": [{"type": "text", "text": "done"}],
                                "usage": {"input": 3, "output": 2, "totalTokens": 5},
                            },
                        }
                    )
                    + "\n"
                )
        return result


class CliOnlyReplyRuntime(FakeDockerRuntime):
    """A transcript with a real CLI reply and no provider original."""

    def run(self, args, timeout=None):
        result = super().run(args, timeout=timeout)
        args = list(args)
        if args and args[0] == "cp" and f":{TRANSCRIPT_EXPORT_CONTAINER_PATH}" in args[1]:
            path = Path(args[2])
            reply = json.loads(path.read_text(encoding="utf-8"))
            reply["message"]["api"] = "cli"
            user = {
                "type": "message",
                "message": {"role": "user", "content": self.agent_messages[0]},
            }
            path.write_text(
                json.dumps(user) + "\n" + json.dumps(reply) + "\n",
                encoding="utf-8",
            )
        return result


class OpenClawHelpersTests(unittest.TestCase):
    def test_rejects_advanced_thinking_on_known_downgrading_openclaw(self) -> None:
        for level in ("xhigh", "adaptive", "max", "ultra"):
            with self.subTest(level=level):
                with self.assertRaisesRegex(RuntimeError, "silently downgrade"):
                    validate_openclaw_thinking(
                        "OpenClaw 2026.3.11 (29dc654)", level
                    )
        validate_openclaw_thinking("OpenClaw 2026.3.11 (29dc654)", "high")
        with self.assertRaisesRegex(RuntimeError, "does not accept"):
            validate_openclaw_thinking("OpenClaw 2026.7.1", "ultra")
        validate_openclaw_thinking("OpenClaw 2026.7.1", "max")
        validate_openclaw_thinking("OpenClaw 2026.8.0", "max")

    def test_embedded_legacy_response_extractor_is_valid_python(self) -> None:
        script = OpenClawHarness._LEGACY_LAST_ASSISTANT_SCRIPT.replace(
            "__ROLLER_SESSION_FILE__", repr("roller.jsonl")
        )
        compile(script, "<response-extractor>", "exec")

    def test_defaults_to_stable_official_openclaw_image(self) -> None:
        self.assertEqual(OpenClawConfig().docker_image, DEFAULT_OPENCLAW_IMAGE)

    def test_session_scope_matches_explicit_session_id_key(self) -> None:
        harness = OpenClawHarness(OpenClawConfig(session_id="roller"), worker_slot=0)
        self.assertEqual(
            harness._session_scope(),
            {
                "agentId": "main",
                "sessionId": "roller",
                "sessionKey": "agent:main:explicit:roller",
            },
        )

    def test_state_dir_keeps_legacy_image_compatibility(self) -> None:
        self.assertEqual(
            OpenClawHarness._state_dir_for_version("OpenClaw 2026.3.11 (29dc654)"),
            "/root/.openclaw",
        )
        self.assertEqual(
            OpenClawHarness._state_dir_for_version("OpenClaw 2026.7.1"),
            OPENCLAW_STATE_CONTAINER_PATH,
        )

    def test_expands_model_config_environment(self) -> None:
        value = {"providers": {"proxy": {"apiKey": "${API_KEY}", "other": 1}}}
        expanded = expand_env_placeholders(value, {"API_KEY": "secret"})
        self.assertEqual(expanded["providers"]["proxy"]["apiKey"], "secret")
        with self.assertRaisesRegex(ValueError, "missing environment variable"):
            expand_env_placeholders(value, {})

    def test_load_model_config_requires_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "models.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be a JSON object"):
                load_model_config(path)

    def test_prepares_selected_model_for_reasoning_without_mutating_source(self) -> None:
        config = {
            "providers": {
                "proxy": {
                    "baseUrl": "http://example.test/v1",
                    "models": [
                        {"id": "plain", "name": "Plain"},
                        {"id": "think", "name": "Think", "tags": ["reasoning"]},
                    ],
                }
            }
        }

        prepared = prepare_model_config(
            config, "proxy/plain", enable_reasoning=True
        )

        models = prepared["providers"]["proxy"]["models"]
        self.assertTrue(models[0]["reasoning"])
        self.assertTrue(models[1]["reasoning"])
        self.assertNotIn("tags", models[1])
        self.assertNotIn("reasoning", config["providers"]["proxy"]["models"][1])
        self.assertEqual(config["providers"]["proxy"]["models"][1]["tags"], ["reasoning"])


    def test_no_keep_thinking_requires_interceptable_model_config(self) -> None:
        task = Task(id="task", prompts=("hello",), source_index=0)
        request = RunRequest(0, task, "provider/model", 30)
        harness = OpenClawHarness(
            OpenClawConfig(
                docker_image="test-image",
                wire_trace_mode="off",
                keep_thinking=False,
            ),
            worker_slot=0,
            runtime=FakeDockerRuntime(),
        )

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "requires --model-config"):
                harness._start_wire_proxy(request, Path(temporary))

    def test_cache_trace_redaction_failure_removes_unsanitized_file(self) -> None:
        harness = OpenClawHarness(
            OpenClawConfig(docker_image="test-image"),
            worker_slot=0,
            runtime=FakeCacheTraceRuntime(),
        )

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "cache_trace.jsonl"
            with mock.patch(
                "roller.harnesses.openclaw.redact_jsonl_file",
                side_effect=RuntimeError("redaction failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "redaction failed"):
                    harness._collect_cache_trace("container", Path(temporary))
            self.assertFalse(destination.exists())

    def test_extracts_usage_from_raw_trace(self) -> None:
        events = [
            {"type": "message", "message": {"role": "user", "content": "hello"}},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "usage": {
                        "input": 10,
                        "output": 4,
                        "cacheRead": 3,
                        "cacheWrite": 2,
                        "totalTokens": 19,
                        "cost": {"total": 0.12},
                    },
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "usage": {"input": 2, "output": 1, "cost": {"total": 0.03}},
                },
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            trace = Path(temporary) / "trace.jsonl"
            trace.write_text(
                "\n".join(json.dumps(event) for event in events) + "\nnot-json\n",
                encoding="utf-8",
            )
            usage = extract_openclaw_usage(trace)

        self.assertEqual(usage["input_tokens"], 12)
        self.assertEqual(usage["output_tokens"], 5)
        self.assertEqual(usage["total_tokens"], 22)
        self.assertEqual(usage["request_count"], 2)
        self.assertEqual(usage["cost_usd"], 0.15)

    def test_extracts_usage_skipping_cli_delivery_copies(self) -> None:
        reply_usage = {
            "input": 16000,
            "output": 677,
            "totalTokens": 16677,
            "cost": {"total": 0.05},
        }
        events = [
            {"type": "message", "message": {"role": "user", "content": "hello"}},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "api": "openai-completions",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": reply_usage,
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "api": "cli",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": dict(reply_usage),
                },
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            trace = Path(temporary) / "trace.jsonl"
            trace.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            usage = extract_openclaw_usage(trace)

        self.assertEqual(usage["request_count"], 1)
        self.assertEqual(usage["input_tokens"], 16000)
        self.assertEqual(usage["output_tokens"], 677)
        self.assertEqual(usage["total_tokens"], 16677)
        self.assertEqual(usage["cost_usd"], 0.05)

    def test_missing_total_tokens_falls_back_to_cache_inclusive_sum(self) -> None:
        events = [
            {"type": "message", "message": {"role": "user", "content": "hello"}},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "api": "openai-completions",
                    "provider": "provider",
                    "model": "model",
                    "content": [{"type": "text", "text": "done"}],
                    "stopReason": "stop",
                    "usage": {"input": 10, "output": 2, "cacheRead": 3, "cacheWrite": 1},
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "api": "cli",
                    "provider": "provider",
                    "model": "model",
                    "content": [{"type": "text", "text": "done"}],
                    "stopReason": "stop",
                    "usage": {
                        "input": 10,
                        "output": 2,
                        "cacheRead": 3,
                        "cacheWrite": 1,
                        "totalTokens": 16,
                    },
                },
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            trace = Path(temporary) / "trace.jsonl"
            trace.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            usage = extract_openclaw_usage(trace)

        # One model request: the mirror is deduplicated and the provider's
        # fallback total is cache-inclusive, matching the CLI's explicit 16
        # instead of the old input+output fallback (12) or a double count (28).
        self.assertEqual(usage["request_count"], 1)
        self.assertEqual(usage["total_tokens"], 16)
        self.assertEqual(usage["cache_read_tokens"], 3)
        self.assertEqual(usage["cache_write_tokens"], 1)

    def test_harness_collects_raw_trace_and_sends_messages_without_shell_quoting(self) -> None:
        task = Task(
            id="task",
            prompts=("Use $HOME and 'quotes' literally", "Finish"),
            source_index=0,
            setup_commands=("true",),
        )
        request = RunRequest(
            index=0,
            task=task,
            model="provider/model",
            timeout_seconds=30,
        )
        runtime = FakeDockerRuntime()
        harness = OpenClawHarness(
            OpenClawConfig(docker_image="test-image", set_image_model=False),
            worker_slot=0,
            runtime=runtime,
        )

        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            result = harness.run(request, output_dir, attempt=1)
            canonical_trace = (output_dir / "rollout" / "trace.jsonl").read_text(
                encoding="utf-8"
            )

        self.assertEqual(result.status, "ok")
        self.assertEqual(runtime.agent_messages, ["Use $HOME and 'quotes' literally", "Finish"])
        self.assertIn('"assistant"', canonical_trace)
        self.assertEqual(result.usage["total_tokens"], 5)
        flattened_commands = [" ".join(command) for command in runtime.commands]
        self.assertTrue(
            any(
                f"OPENCLAW_STATE_DIR={OPENCLAW_STATE_CONTAINER_PATH}" in command
                for command in flattened_commands
            )
        )
        container_command = next(command for command in runtime.commands if command[:2] == ["run", "-d"])
        self.assertEqual(container_command[container_command.index("--user") + 1], "0")
        self.assertTrue(
            any("ROLLER_SDK_EXPORT_TRANSCRIPT" in command for command in flattened_commands)
        )
        self.assertTrue(
            any("openclaw config set gateway.mode local" in command for command in flattened_commands)
        )
        self.assertTrue(
            any("openclaw config set gateway.bind loopback" in command for command in flattened_commands)
        )
        self.assertTrue(
            any("ROLLER_GATEWAY_PORT=18789" in command for command in flattened_commands)
        )

    def test_harness_counts_single_request_when_transcript_has_cli_delivery_copy(self) -> None:
        task = Task(id="task", prompts=("Summarize inputs",), source_index=0)
        request = RunRequest(
            index=0,
            task=task,
            model="provider/model",
            timeout_seconds=30,
        )
        runtime = CliDeliveryCopyRuntime()
        harness = OpenClawHarness(
            OpenClawConfig(docker_image="test-image", set_image_model=False),
            worker_slot=0,
            runtime=runtime,
        )

        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            result = harness.run(request, output_dir, attempt=1)
            raw_trace = (output_dir / "rollout" / "trace.jsonl").read_text(
                encoding="utf-8"
            )
            trajectory = json.loads(
                (output_dir / "rollout" / "trace_ACTF.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(result.status, "ok")
        # The raw trace keeps both events for traceability...
        self.assertEqual(raw_trace.count('"assistant"'), 2)
        # ...while usage and the ACTF trajectory count the reply once.
        self.assertEqual(result.usage["request_count"], 1)
        self.assertEqual(result.usage["total_tokens"], 5)
        self.assertEqual(len(trajectory["steps"]), 1)

    def test_trace_conversion_failure_does_not_invalidate_evaluation(self) -> None:
        task = Task(id="task", prompts=("Summarize inputs",), source_index=0)
        request = RunRequest(0, task, "provider/model", 30)
        harness = OpenClawHarness(
            OpenClawConfig(docker_image="test-image", set_image_model=False),
            worker_slot=0,
            runtime=FakeDockerRuntime(),
        )

        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch(
                "roller.harnesses.openclaw.convert_openclaw_trace",
                side_effect=ValueError("invalid trace"),
            ),
        ):
            result = harness.run(request, Path(temporary), attempt=1)

        self.assertEqual(result.status, "ok", result.error)
        self.assertIsNone(result.trajectory_path)
        self.assertEqual(
            result.metadata["trajectory_conversion"],
            {
                "status": "unavailable",
                "target": "actf",
                "error": "invalid trace",
            },
        )

    def test_cli_only_reply_finalizes_as_successful_sft_sample(self) -> None:
        from roller.collector import CollectionConfig, Collector

        request = RunRequest(0, Task("cli-only", ("Summarize inputs",), 0), "provider/model", 30)
        harness = OpenClawHarness(
            OpenClawConfig(docker_image="test-image", set_image_model=False),
            worker_slot=0,
            runtime=CliOnlyReplyRuntime(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            collector = Collector(CollectionConfig(root), lambda slot: harness)
            result = collector.collect([request])[0]
            self.assertEqual(result["status"], "ok", result.get("error"))
            self.assertEqual(result["usage"]["request_count"], 1)
            self.assertEqual(result["usage"]["total_tokens"], 5)
            sft = json.loads((root / "sft.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(
                [message["role"] for message in sft["messages"]],
                ["system", "user", "assistant"],
            )
            self.assertEqual(sft["messages"][-1]["content"], "done")

    def test_harness_passes_thinking_to_each_agent_turn_and_clears_retry_traces(self) -> None:
        task = Task(id="task", prompts=("first", "second"), source_index=0)
        request = RunRequest(0, task, "provider/model", 30)
        runtime = FakeDockerRuntime()
        harness = OpenClawHarness(
            OpenClawConfig(
                docker_image="test-image",
                thinking="high",
                wire_trace_mode="off",
                set_image_model=False,
            ),
            worker_slot=0,
            runtime=runtime,
        )

        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            (output_dir / "rollout").mkdir()
            (output_dir / "cache_trace.jsonl").write_text("stale", encoding="utf-8")
            (output_dir / "wire_trace.jsonl").write_text("stale", encoding="utf-8")
            (output_dir / "rollout" / "wire_strip.jsonl").write_text(
                "stale", encoding="utf-8"
            )

            result = harness.run(request, output_dir, attempt=2)

            self.assertFalse((output_dir / "cache_trace.jsonl").exists())
            self.assertFalse((output_dir / "wire_trace.jsonl").exists())
            self.assertFalse((output_dir / "rollout" / "wire_strip.jsonl").exists())

        self.assertEqual(result.status, "ok")
        agent_commands = [command for command in runtime.commands if "agent" in command]
        self.assertEqual(len(agent_commands), 2)
        gateway_command = next(
            command
            for command in runtime.commands
            if any("openclaw gateway" in argument for argument in command)
        )
        self.assertIn("/roller/task/environment", gateway_command[-1])
        self.assertIn("exec openclaw gateway", gateway_command[-1])
        gateway_token = next(
            value for value in gateway_command if value.startswith("OPENCLAW_GATEWAY_TOKEN=")
        )
        for command in agent_commands:
            self.assertEqual(command[command.index("--thinking") + 1], "high")
            self.assertIn(gateway_token, command)

    def test_harness_passes_task_max_override_without_downgrading(self) -> None:
        task = Task(
            id="task",
            prompts=("solve",),
            source_index=0,
            thinking="max",
        )
        request = RunRequest(0, task, "provider/model", 30)
        runtime = FakeDockerRuntime()
        harness = OpenClawHarness(
            OpenClawConfig(
                docker_image="test-image",
                thinking="low",
                wire_trace_mode="off",
                set_image_model=False,
            ),
            worker_slot=0,
            runtime=runtime,
        )

        with tempfile.TemporaryDirectory() as temporary:
            result = harness.run(request, Path(temporary), attempt=1)

        self.assertEqual(result.status, "ok")
        config_command = next(
            command
            for command in runtime.commands
            if "agents.defaults.thinkingDefault" in command
        )
        self.assertEqual(config_command[-1], "max")
        agent_command = next(
            command for command in runtime.commands if "agent" in command
        )
        self.assertEqual(agent_command[agent_command.index("--thinking") + 1], "max")

    def test_markdown_inside_one_message_remains_one_turn(self) -> None:
        prompt = "## Round 1\nKeep this whole message.\n## Round 2\nStill the same message."
        task = Task(id="task", prompts=(prompt,), source_index=0)
        request = RunRequest(0, task, "provider/model", 30)
        harness = OpenClawHarness(
            OpenClawConfig(docker_image="test-image"),
            worker_slot=0,
            runtime=FakeDockerRuntime(),
        )

        self.assertEqual(harness._prompts(request), (prompt,))

    def test_new_session_boundary_uses_fresh_id_and_keeps_one_container(self) -> None:
        runtime = FakeDockerRuntime()
        task = Task(
            id="task",
            prompts=("first", "second"),
            source_index=0,
            new_session=(False, True),
        )
        request = RunRequest(0, task, "provider/model", 30)
        harness = OpenClawHarness(
            OpenClawConfig(docker_image="test-image", set_image_model=False),
            worker_slot=0,
            runtime=runtime,
        )

        with tempfile.TemporaryDirectory() as temporary:
            result = harness.run(request, Path(temporary), attempt=1)

        self.assertEqual(result.status, "ok", result.error)
        agent_commands = [command for command in runtime.commands if "agent" in command]
        self.assertEqual(len(agent_commands), 2)
        self.assertEqual(
            [command[command.index("--session-id") + 1] for command in agent_commands],
            ["roller", "roller-2"],
        )
        starts = [command for command in runtime.commands if command[:2] == ["run", "-d"]]
        self.assertEqual(len(starts), 1)

    def test_retained_error_container_is_stopped_to_release_gateway_port(self) -> None:
        runtime = FakeDockerRuntime()
        request = RunRequest(0, Task("task", ("first",), 0), "provider/model", 30)
        harness = OpenClawHarness(
            OpenClawConfig(
                docker_image="test-image",
                set_image_model=False,
                keep_container_on_error=True,
            ),
            worker_slot=0,
            runtime=runtime,
        )

        with tempfile.TemporaryDirectory() as temporary:
            session = harness.create_session(request, Path(temporary), attempt=1)
            session.start()
            result = session.finish("error", "test failure")

        self.assertEqual(result.status, "error")
        self.assertEqual(result.metadata["container"], session.container)
        self.assertIn(["stop", "--time", "1", session.container], runtime.commands)
        self.assertNotIn(["rm", "-f", session.container], runtime.commands)

    def test_harness_mounts_bundle_injects_context_and_collects_all_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "inputs").mkdir()
            (root / "inputs" / "input.txt").write_text("input", encoding="utf-8")
            (root / "SYSTEM.md").write_text("system", encoding="utf-8")
            tasks_path = root / "tasks.json"
            tasks_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "bundle",
                            "prompt": "write outputs/report.md",
                            "files": {
                                "output_dir": "deliverables",
                                "inputs": ["inputs/input.txt"],
                                "context": [{"role": "system", "source": "SYSTEM.md"}],
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )
            task = load_tasks(tasks_path)[0]
            store = TaskBundleStore(root / "run")
            bundle = store.prepare(task)
            request = RunRequest(0, task, "provider/model", 30, bundle=bundle)
            runtime = FakeDockerRuntime()
            harness = OpenClawHarness(
                OpenClawConfig(docker_image="test-image", set_image_model=False),
                worker_slot=0,
                runtime=runtime,
            )
            output_dir = root / "sample"
            result = harness.run(request, output_dir, attempt=1)

            flattened_commands = [" ".join(command) for command in runtime.commands]

            self.assertEqual(result.status, "ok")
            self.assertEqual(
                result.metadata["provenance"]["task_bundle_digest"],
                bundle.manifest_digest,
            )
            self.assertTrue(result.metadata["output_collection"]["passed"])
            self.assertEqual(
                result.metadata["output_collection"]["agent_output_dir"],
                "deliverables",
            )
            self.assertEqual(result.metadata["output_collection"]["file_count"], 2)
            self.assertTrue((output_dir / "results" / "report.md").is_file())
            self.assertTrue((output_dir / "results" / "extra.log").is_file())
            self.assertTrue(any("/roller/task/inputs:ro" in command for command in flattened_commands))
            self.assertTrue(
                any(
                    "/tmp_workspace/inputs/input.txt:ro" in command
                    for command in flattened_commands
                )
            )
            self.assertTrue(
                any(
                    '/roller/task/context/. "$HOME"/' in command
                    for command in flattened_commands
                )
            )
            self.assertTrue(
                any(
                    "openclaw config set agents.defaults.workspace /tmp_workspace" in command
                    for command in flattened_commands
                )
            )

    def test_copy_input_mode_uses_a_writable_workspace_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "seed.txt").write_text("before", encoding="utf-8")
            tasks_path = root / "tasks.json"
            tasks_path.write_text(
                json.dumps(
                    [{
                        "id": "editable",
                        "prompt": "edit seed.txt",
                        "files": {
                            "inputs": ["seed.txt"],
                            "input_mode": "copy",
                        },
                    }]
                ),
                encoding="utf-8",
            )
            task = load_tasks(tasks_path)[0]
            bundle = TaskBundleStore(root / "run").prepare(task)
            request = RunRequest(0, task, "provider/model", 30, bundle=bundle)
            runtime = FakeDockerRuntime()
            harness = OpenClawHarness(
                OpenClawConfig(docker_image="test-image", set_image_model=False),
                worker_slot=0,
                runtime=runtime,
            )

            result = harness.run(request, root / "sample", attempt=1)
            commands = [" ".join(command) for command in runtime.commands]

            self.assertEqual(result.status, "ok", result.error)
            self.assertFalse(
                any("/tmp_workspace/seed.txt:ro" in command for command in commands)
            )
            self.assertTrue(
                any(
                    "cp -a /roller/task/inputs/. /tmp_workspace/" in command
                    and "chmod -R u+w /tmp_workspace" in command
                    for command in commands
                )
            )


if __name__ == "__main__":
    unittest.main()
