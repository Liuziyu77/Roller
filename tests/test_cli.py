from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from roller.cli import _build_parser, _collect, _docker_preflight, _jsonl_equivalent, _validate_thinking_requests
from roller.models import RunRequest, Task, UserSimulatorSpec
from roller.thinking_capabilities import ThinkingCapabilities, ThinkingCapability
from roller.harnesses.codex import codex_effort


class CliTests(unittest.TestCase):
    def test_review_preflight_matrix_uses_runtime_endpoint_and_codex_rules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tasks = root / "tasks.jsonl"
            tasks.write_text(json.dumps({"id": "hello", "prompt": "say hello"}) + "\n")
            capabilities = root / "capabilities.json"
            capabilities.write_text(json.dumps({
                "schema_version": "roller.thinking-capabilities/v1",
                "profiles": [{"harness": "claude-code", "model": "served-model",
                              "api_base": "http://gateway.test", "supported_efforts": ["low"]}],
            }))
            for endpoint in ("http://gateway.test", "http://gateway.test/v1",
                             "http://gateway.test/v1/messages"):
                args = _build_parser().parse_args([
                    "collect", "--tasks", str(tasks), "--harness", "claude-code",
                    "--model", "served-model", "--thinking", "high",
                    "--claude-api-base", endpoint,
                    "--thinking-capabilities", str(capabilities), "--dry-run",
                ])
                with self.subTest(endpoint=endpoint), self.assertRaisesRegex(ValueError, "not declared supported"):
                    _collect(args)
            for effort in ("off", "adaptive"):
                args = _build_parser().parse_args([
                    "collect", "--tasks", str(tasks), "--harness", "codex",
                    "--model", "served-model", "--thinking", effort, "--dry-run",
                ])
                with self.subTest(effort=effort), self.assertRaises(ValueError):
                    _collect(args)

    def test_codex_preflight_rejects_runtime_unsupported_efforts(self):
        for effort in ("off", "adaptive"):
            with self.subTest(effort=effort), self.assertRaises(ValueError):
                request = RunRequest(0, Task(id="task", prompts=("hello",), source_index=0,
                                             thinking=effort), "model", 30)
                _validate_thinking_requests([request], cli_harness="codex", harness="codex",
                                            api_base=None, default_effort=None,
                                            effort_validator=codex_effort, capabilities=None)

    def test_capabilities_use_logical_effort_for_simulator_and_resolve_endpoint(self):
        profiles = ThinkingCapabilities((ThinkingCapability(
            harness="claude-code", model="sim-model", supported_efforts=frozenset({"ultra"}),
            api_base="http://gateway.test",
        ),))
        task = Task(id="task", prompts=("hello",), source_index=0,
                    user_simulator=UserSimulatorSpec(instruction="test", harness="claude-code", model="sim-model", thinking="ultra"))
        request = RunRequest(0, task, "agent-model", 30)
        _validate_thinking_requests(
            [request], cli_harness="pi", harness="claude-code",
            api_base=lambda model: "http://gateway.test/", default_effort="low",
            effort_validator=lambda effort: "ultracode", capabilities=profiles,
        )
        denied = ThinkingCapabilities((ThinkingCapability(
            harness="claude-code", model="sim-model", supported_efforts=frozenset({"low"}),
        ),))
        with self.assertRaisesRegex(ValueError, "not declared supported"):
            _validate_thinking_requests(
                [request], cli_harness="pi", harness="claude-code",
                api_base=None, default_effort="low",
                effort_validator=lambda effort: "ultracode", capabilities=denied,
            )

    def test_retired_cot_flags_are_rejected(self) -> None:
        for flag, value in (
            ("--external-thinking", "external"),
            ("--external-thinking", "external_always"),
            ("--external-thinking", "off"),
            ("--claude-signature-recovery", "fill"),
            ("--claude-signature-recovery", "prefer"),
            ("--claude-signature-recovery-attempts", "2"),
            ("--claude-signature-recovery-max-tokens", "8192"),
        ):
            with self.subTest(flag=flag, value=value):
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
                    _build_parser().parse_args(["collect", "--tasks", "tasks.jsonl", flag, value])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("unrecognized arguments", errors.getvalue())

    def test_jsonl_equivalence_ignores_object_key_order(self) -> None:
        self.assertTrue(
            _jsonl_equivalent(
                b'{"id":"one","runtime":{"image":"test","args":{"a":1,"b":2}}}\n',
                b'{"runtime":{"args":{"b":2,"a":1},"image":"test"},"id":"one"}\n',
            )
        )
        self.assertFalse(
            _jsonl_equivalent(b'{"id":"one"}\n', b'{"id":"two"}\n')
        )

    def test_parser_accepts_samples_grader_parallel_and_bench_timeout_scale(self) -> None:
        collect = _build_parser().parse_args(
            [
                "collect", "--tasks", "tasks.jsonl", "--samples", "3",
                "--grader-parallel", "2",
            ]
        )
        export = _build_parser().parse_args(
            [
                "bench", "export", "pinchbench", "/bench", "-o", "tasks.jsonl",
                "--timeout-multiplier", "2.5",
            ]
        )
        self.assertEqual((collect.samples, collect.grader_parallel), (3, 2))
        self.assertEqual(collect.gateway_startup_timeout, 240.0)
        self.assertEqual(export.timeout_multiplier, 2.5)

    def test_contextual_skillsbench_defaults_are_applied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "tasks" / "demo"
            (task / "environment" / "skills").mkdir(parents=True)
            (task / "verifier").mkdir()
            (task / "environment" / "Dockerfile").write_text(
                "FROM python:3.12-slim\nWORKDIR /root\n", encoding="utf-8"
            )
            (task / "verifier" / "test.sh").write_text(
                "echo 1 > /logs/verifier/reward.txt\n", encoding="utf-8"
            )
            (task / "task.md").write_text(
                "---\nmetadata:\n  category: demo\n"
                "agent:\n  timeout_sec: 30\n"
                "verifier:\n  timeout_sec: 30\n"
                "environment:\n  build_timeout_sec: 30\n"
                "---\n\nDo it.\n",
                encoding="utf-8",
            )
            args = _build_parser().parse_args(
                [
                    "collect",
                    "--bench",
                    "skillsbench",
                    "--bench-root",
                    str(root),
                    "--model",
                    "test-model",
                    "--dry-run",
                ]
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(_collect(args), 0)
            payload = json.loads(output.getvalue())

        self.assertEqual(payload["samples"], 3)
        self.assertEqual(payload["trajectories"], 3)
        self.assertEqual(payload["opencode_max_steps"], 100)
        self.assertEqual(payload["opencode_output_tokens"], 32768)
        self.assertTrue(payload["opencode_vision"])

    def test_parser_requires_exactly_one_task_source(self) -> None:
        parser = _build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["collect", "--dry-run"])
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "collect",
                    "--tasks",
                    "tasks.jsonl",
                    "--bench",
                    "pinchbench",
                ]
            )

    def test_one_command_bench_dry_run_loads_selection_and_task_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tasks_dir = root / "tasks"
            assets_dir = root / "assets"
            tasks_dir.mkdir()
            assets_dir.mkdir()
            (tasks_dir / "manifest.yaml").write_text(
                "run_first: [task_sanity]\ncategories:\n  setup: [task_sanity]\n",
                encoding="utf-8",
            )
            (tasks_dir / "task_sanity.md").write_text(
                """---
id: task_sanity
name: Sanity
category: setup
grading_type: automated
timeout_seconds: 45
---

## Prompt

Say hello.

## Automated Checks

```python
def grade(transcript, workspace_path):
    return {"ok": 1.0}
```
""",
                encoding="utf-8",
            )
            args = _build_parser().parse_args(
                [
                    "collect",
                    "--bench",
                    "pinchbench",
                    "--bench-root",
                    str(root),
                    "--bench-task-id",
                    "task_sanity",
                    "--model",
                    "provider/model",
                    "--timeout",
                    "5",
                    "--dry-run",
                ]
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = _collect(args)

        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["bench"]["name"], "pinchbench")
        self.assertEqual(payload["bench"]["task_ids"], ["task_sanity"])
        self.assertEqual(payload["tasks"], 1)
        self.assertEqual(payload["plan"][0]["timeout_seconds"], 45)

    def test_dry_run_model_label_preserves_upstream_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tasks = Path(temporary) / "tasks.jsonl"
            tasks.write_text(
                json.dumps({"id": "hello", "prompt": "say hello"}) + "\n",
                encoding="utf-8",
            )
            args = _build_parser().parse_args(
                [
                    "collect",
                    "--tasks",
                    str(tasks),
                    "--model",
                    "provider/external-served-id",
                    "--model-label",
                    "display-model",
                    "--dry-run",
                ]
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                _collect(args)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["models"], ["provider/external-served-id"])
        self.assertEqual(payload["model_labels"], ["display-model"])
        self.assertEqual(payload["plan"][0]["model"], "provider/external-served-id")
        self.assertEqual(payload["plan"][0]["model_label"], "display-model")

    def test_collect_help_groups_shared_options_by_scope(self) -> None:
        parser = _build_parser()
        collect_parser = parser._subparsers._group_actions[0].choices["collect"]
        groups = {
            group.title: {action.dest for action in group._group_actions}
            for group in collect_parser._action_groups
        }

        self.assertIn("thinking", groups["Harness runtime and trajectory"])
        self.assertIn("thinking_capabilities", groups["Harness runtime and trajectory"])
        self.assertIn("wire_trace", groups["Harness runtime and trajectory"])
        self.assertIn("model_config", groups["OpenClaw"])
        self.assertIn("openclaw_model_request_timeout", groups["OpenClaw"])
        self.assertIn("openclaw_max_tokens", groups["OpenClaw"])
        self.assertNotIn("thinking", groups["OpenClaw"])
        self.assertIn("api_base", groups["Harness API"])
        self.assertIn("api_key_env", groups["Harness API"])
        self.assertIn("pi_api_base", groups["Harness API"])

    def test_thinking_capabilities_reject_unsupported_effort_before_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tasks = root / "tasks.jsonl"
            tasks.write_text(
                json.dumps({"id": "hello", "prompt": "say hello"}) + "\n",
                encoding="utf-8",
            )
            model_config = root / "models.json"
            model_config.write_text(json.dumps({"providers": {"test": {
                "baseUrl": "http://gateway.test/v1", "api": "openai-completions",
                "models": [{"id": "served-model", "name": "served-model"}],
            }}}))
            for harness in ("hermes", "opencode", "deepseek", "pi", "codex", "claude-code", "openclaw"):
                with self.subTest(harness=harness):
                    model = "test/served-model" if harness == "openclaw" else "served-model"
                    capabilities = root / f"{harness}-capabilities.json"
                    capabilities.write_text(
                        json.dumps(
                            {
                                "schema_version": "roller.thinking-capabilities/v1",
                                "profiles": [
                                    {
                                        "harness": harness,
                                        "model": model,
                                        "api_base": "http://gateway.test/v1",
                                        "supported_efforts": ["low"],
                                    }
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                    args = _build_parser().parse_args(
                        [
                            "collect",
                            "--tasks",
                            str(tasks),
                            "--harness",
                            harness,
                            "--model",
                            model,
                            "--api-base",
                            "http://gateway.test/v1/",
                            "--thinking",
                            "high",
                            "--thinking-capabilities",
                            str(capabilities),
                            "--dry-run",
                        ] + (["--model-config", str(model_config)] if harness == "openclaw" else [])
                    )
                    with self.assertRaisesRegex(
                        ValueError, "not declared supported"
                    ):
                        _collect(args)

    def test_parser_accepts_codex_harness_options(self) -> None:
        args = _build_parser().parse_args(
            [
                "collect",
                "--tasks",
                "tasks.jsonl",
                "--harness",
                "codex",
                "--api-key-env",
                "ROLLER_CODEX_KEY",
                "--api-base",
                "http://example.test/v1",
                "--thinking",
                "xhigh",
                "--reasoning-summary",
                "detailed",
                "--codex-websockets",
            ]
        )
        self.assertEqual(args.harness, "codex")
        self.assertEqual(args.api_key_env, "ROLLER_CODEX_KEY")
        self.assertEqual(args.thinking, "xhigh")
        self.assertEqual(args.reasoning_summary, "detailed")
        self.assertTrue(args.codex_websockets)

    def test_parser_accepts_openclaw_model_request_timeout(self) -> None:
        args = _build_parser().parse_args(
            [
                "collect",
                "--tasks",
                "tasks.jsonl",
                "--harness",
                "openclaw",
                "--model-config",
                "models.json",
                "--openclaw-model-request-timeout",
                "300",
                "--openclaw-max-tokens",
                "32768",
            ]
        )

        self.assertEqual(args.openclaw_model_request_timeout, 300)
        self.assertEqual(args.openclaw_max_tokens, 32768)

    def test_parser_accepts_claude_code_harness_options(self) -> None:
        args = _build_parser().parse_args(
            [
                "collect",
                "--tasks",
                "tasks.jsonl",
                "--harness",
                "claude-code",
                "--api-key-env",
                "ROLLER_ANTHROPIC_KEY",
                "--anthropic-base-url",
                "http://example.test/v1/messages",
                "--thinking",
                "high",
                "--claude-max-turns",
                "20",
                "--claude-max-thinking-tokens",
                "4096",
                "--claude-permission-mode",
                "bypassPermissions",
            ]
        )
        self.assertEqual(args.harness, "claude-code")
        self.assertEqual(args.api_key_env, "ROLLER_ANTHROPIC_KEY")
        self.assertEqual(args.api_base, "http://example.test/v1/messages")
        self.assertEqual(args.thinking, "high")
        self.assertEqual(args.claude_max_turns, 20)
        self.assertEqual(args.claude_max_thinking_tokens, 4096)

    def test_claude_code_dry_run_normalizes_without_docker_or_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tasks = Path(temporary) / "tasks.jsonl"
            tasks.write_text(
                json.dumps({"id": "hello", "prompt": "say hello"}) + "\n",
                encoding="utf-8",
            )
            args = _build_parser().parse_args(
                [
                    "collect",
                    "--tasks",
                    str(tasks),
                    "--harness",
                    "claude-code",
                    "--model",
                    "claude-sonnet-4-6-thinking",
                    "--api-base",
                    "http://example.test/v1/messages",
                    "--thinking",
                    "high",
                    "--dry-run",
                ]
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = _collect(args)
        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["harness"], "claude-code")
        self.assertEqual(payload["trajectories"], 1)
        self.assertEqual(payload["plan"][0]["turns"]["count"], 1)


    def test_task_harness_is_used_when_cli_harness_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tasks = Path(temporary) / "tasks.jsonl"
            tasks.write_text(
                json.dumps(
                    {
                        "id": "hello",
                        "harness": "codex",
                        "model": "gpt-model",
                        "prompt": "say hello",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            args = _build_parser().parse_args(
                ["collect", "--tasks", str(tasks), "--dry-run"]
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = _collect(args)
        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["harness"], "codex")
        self.assertEqual(payload["plan"][0]["harness"], "codex")

    @patch("roller.cli.subprocess.run")
    @patch("roller.cli.shutil.which", return_value="/usr/bin/docker")
    def test_docker_preflight_returns_resolved_image_id(self, _which, run) -> None:
        run.side_effect = [
            subprocess.CompletedProcess([], 0, "27.5.1\n", ""),
            subprocess.CompletedProcess([], 0, "sha256:image-id\n", ""),
        ]

        digest = _docker_preflight("docker", "openclaw:test")

        self.assertEqual(digest, "sha256:image-id")

    @patch("roller.cli.subprocess.run")
    @patch("roller.cli.shutil.which", return_value="/usr/bin/docker")
    def test_docker_preflight_requires_a_local_image(self, _which, run) -> None:
        run.side_effect = [
            subprocess.CompletedProcess([], 0, "27.5.1\n", ""),
            subprocess.CompletedProcess([], 1, "", "No such image"),
        ]

        with self.assertRaisesRegex(RuntimeError, "pull or load it first"):
            _docker_preflight("docker", "missing:test")


if __name__ == "__main__":
    unittest.main()
