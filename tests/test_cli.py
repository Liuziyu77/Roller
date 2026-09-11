from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from roller.cli import _build_parser, _collect, _docker_preflight


class CliTests(unittest.TestCase):
    def test_collect_help_groups_shared_options_by_scope(self) -> None:
        parser = _build_parser()
        collect_parser = parser._subparsers._group_actions[0].choices["collect"]
        groups = {
            group.title: {action.dest for action in group._group_actions}
            for group in collect_parser._action_groups
        }

        self.assertIn("thinking", groups["Harness runtime and trajectory"])
        self.assertIn("wire_trace", groups["Harness runtime and trajectory"])
        self.assertIn("model_config", groups["OpenClaw"])
        self.assertNotIn("thinking", groups["OpenClaw"])
        self.assertIn("api_base", groups["Codex, Claude Code and Hermes API"])
        self.assertIn("api_key_env", groups["Codex, Claude Code and Hermes API"])

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
                "--external-thinking",
                "external_always",
                "--codex-websockets",
            ]
        )
        self.assertEqual(args.harness, "codex")
        self.assertEqual(args.api_key_env, "ROLLER_CODEX_KEY")
        self.assertEqual(args.thinking, "xhigh")
        self.assertEqual(args.reasoning_summary, "detailed")
        self.assertEqual(args.external_thinking, "external_always")
        self.assertTrue(args.codex_websockets)

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
                "--claude-signature-recovery",
                "prefer",
                "--claude-signature-recovery-attempts",
                "2",
                "--claude-signature-recovery-max-tokens",
                "12000",
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
        self.assertEqual(args.claude_signature_recovery, "prefer")
        self.assertEqual(args.claude_signature_recovery_attempts, 2)
        self.assertEqual(args.claude_signature_recovery_max_tokens, 12000)

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

    def test_claude_code_rejects_external_thinking_before_docker(self) -> None:
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
                    "--external-thinking",
                    "external",
                    "--dry-run",
                ]
            )
            with self.assertRaisesRegex(ValueError, "not implemented"):
                _collect(args)

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
