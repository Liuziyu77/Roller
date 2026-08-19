from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from roller.bundles import TaskBundleStore
from roller.harnesses.openclaw import (
    OpenClawConfig,
    OpenClawHarness,
    expand_env_placeholders,
    load_model_config,
)
from roller.models import RunRequest, Task
from roller.tasks import load_tasks
from roller.usage import extract_openclaw_usage


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

    def run(self, args, timeout=None):
        args = list(args)
        self.commands.append(args)
        stdout = "2026.1.0\n" if args[-2:] == ["openclaw", "--version"] else ""
        if args and args[0] == "cp" and ":/root/.openclaw/" in args[1]:
            destination = Path(args[2])
            event = {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "usage": {"input": 3, "output": 2, "totalTokens": 5},
                },
            }
            destination.write_text(json.dumps(event) + "\n", encoding="utf-8")
        if args and args[0] == "cp" and ":/tmp_workspace/outputs/." in args[1]:
            destination = Path(args[2])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "report.md").write_text("generated", encoding="utf-8")
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


class OpenClawHelpersTests(unittest.TestCase):
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

    def test_harness_collects_raw_trace_and_sends_messages_without_shell_quoting(self) -> None:
        task = Task(
            id="task",
            prompts=("Use $HOME and 'quotes' literally", "Finish"),
            source_index=0,
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

    def test_harness_mounts_bundle_injects_context_and_validates_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("input", encoding="utf-8")
            (root / "SYSTEM.md").write_text("system", encoding="utf-8")
            tasks_path = root / "tasks.json"
            tasks_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "bundle",
                            "prompt": "write outputs/report.md",
                            "files": {
                                "inputs": ["input.txt"],
                                "context": [{"role": "system", "source": "SYSTEM.md"}],
                                "outputs": [{"path": "report.md", "required": True}],
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

            provenance = json.loads((output_dir / "provenance.json").read_text())
            validation = json.loads((output_dir / "artifact_validation.json").read_text())
            flattened_commands = [" ".join(command) for command in runtime.commands]

            self.assertEqual(result.status, "ok")
            self.assertEqual(provenance["task_bundle_digest"], bundle.manifest_digest)
            self.assertTrue(validation["passed"])
            self.assertTrue(
                (output_dir / "artifacts" / "declared" / "report.md").is_file()
            )
            self.assertTrue(any("/roller/task/inputs:ro" in command for command in flattened_commands))
            self.assertTrue(any("/roller/task/context/. /root/" in command for command in flattened_commands))


if __name__ == "__main__":
    unittest.main()
