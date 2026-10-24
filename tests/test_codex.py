from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from roller.harnesses.codex import (
    CODEX_ENCRYPTED_CONTENT_RETRIES,
    CodexConfig,
    CodexHarness,
    redact_api_base,
)
from roller.models import RunRequest, Task


class CodexHarnessTests(unittest.TestCase):
    def _request(self, thinking: str | None = None) -> RunRequest:
        return RunRequest(
            index=0,
            task=Task(
                id="task",
                prompts=("hello",),
                source_index=0,
                thinking=thinking,
            ),
            model="gpt-5.6-sol",
            timeout_seconds=30,
        )

    def test_maps_named_host_secret_to_openai_api_key(self) -> None:
        harness = CodexHarness(
            CodexConfig(api_key_env="ROLLER_TEST_CODEX_KEY"), 0
        )
        with patch.dict(
            os.environ,
            {"ROLLER_TEST_CODEX_KEY": "secret", "HTTPS_PROXY": "proxy"},
            clear=True,
        ):
            environment = harness._environment(self._request())
        self.assertEqual(environment["OPENAI_API_KEY"], "secret")
        self.assertEqual(environment["HTTPS_PROXY"], "proxy")
        self.assertNotIn("ROLLER_TEST_CODEX_KEY", environment)

    def test_builds_version_stable_exec_command(self) -> None:
        harness = CodexHarness(
            CodexConfig(
                api_base="http://example.test/v1",
                reasoning_effort="high",
                reasoning_summary="detailed",
                web_search="disabled",
                supports_websockets=False,
            ),
            0,
        )
        command = harness._codex_command(
            "container", self._request(), "hello", resume=False
        )
        self.assertIn("--json", command)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertIn('model_reasoning_summary="detailed"', command)
        self.assertIn('model_provider="roller_api"', command)
        self.assertIn(
            'model_providers.roller_api.base_url="http://example.test/v1"',
            command,
        )
        self.assertIn(
            "model_providers.roller_api.supports_websockets=false", command
        )
        self.assertIn("disable_response_storage=true", command)
        self.assertEqual(command[-2:], ["--", "hello"])

    def test_config_set_follows_roller_overrides_on_every_turn(self) -> None:
        harness = CodexHarness(
            CodexConfig(
                api_base="http://example.test/v1",
                config_set=(
                    ("model_providers.roller_api.stream_idle_timeout_ms", "1800000"),
                    ("model_verbosity", '"low"'),
                ),
            ),
            0,
        )
        harness._root_thread_id = "thread"
        for resume in (False, True):
            with self.subTest(resume=resume):
                command = harness._codex_command(
                    "container", self._request(), "hello", resume=resume
                )
                overrides = [
                    command[index + 1]
                    for index, argument in enumerate(command)
                    if argument == "-c"
                ]
                self.assertEqual(
                    overrides[-2:],
                    [
                        "model_providers.roller_api.stream_idle_timeout_ms=1800000",
                        'model_verbosity="low"',
                    ],
                )
                self.assertIn('model_provider="roller_api"', overrides)
                self.assertEqual(command[-2:], ["--", "hello"])

    def test_config_set_rejects_paths_roller_manages(self) -> None:
        for key, hint in (
            ("model_provider", "--codex-api-base"),
            ("model_providers.roller_api", "--codex-api-base"),
            ("model_providers.roller_api.base_url", "--codex-api-base"),
            ("model_reasoning_effort", "--thinking"),
            ("web_search", "--codex-web-search"),
            ("features", "Roller enables it"),
        ):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, hint):
                    CodexConfig(config_set=((key, "1"),))
        with self.assertRaisesRegex(ValueError, "more than once"):
            CodexConfig(config_set=(("model_verbosity", "1"), ("model_verbosity", "2")))

    def test_task_effort_overrides_cli_and_rejects_openclaw_profiles(self) -> None:
        harness = CodexHarness(CodexConfig(reasoning_effort="low"), 0)
        self.assertEqual(harness._effective_effort(self._request("xhigh")), "xhigh")
        with self.assertRaisesRegex(ValueError, "reasoning-off"):
            harness._effective_effort(self._request("off"))
        with self.assertRaisesRegex(ValueError, "adaptive"):
            harness._effective_effort(self._request("adaptive"))
        self.assertEqual(harness._effective_effort(self._request("max")), "max")

    def test_forwards_advanced_effort_to_codex_for_runtime_validation(self) -> None:
        for effort in ("max", "ultra"):
            with self.subTest(effort=effort):
                harness = CodexHarness(CodexConfig(reasoning_effort=effort), 0)
                command = harness._codex_command(
                    "container", self._request(), "hello", resume=False
                )
                self.assertIn(
                    f'model_reasoning_effort="{effort}"', command
                )

    def test_requires_absolute_http_api_base(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute http"):
            CodexConfig(api_base="example.test/v1")


    def test_full_wire_trace_requires_api_base_and_host_network(self) -> None:
        with self.assertRaisesRegex(ValueError, "full requires --api-base"):
            CodexConfig(wire_trace_mode="full")
        with self.assertRaisesRegex(ValueError, "network host"):
            CodexConfig(
                api_base="http://example.test/v1",
                wire_trace_mode="full",
                network=None,
            )

    def test_wire_proxy_retries_encrypted_content_rejections(self) -> None:
        harness = CodexHarness(CodexConfig(api_base="http://example.test/v1"), 0)
        with tempfile.TemporaryDirectory() as temporary, patch(
            "roller.harnesses.codex.WireTraceProxy"
        ) as proxy:
            harness._start_wire_proxy(Path(temporary))
        self.assertEqual(
            proxy.call_args.kwargs["encrypted_content_retries"],
            CODEX_ENCRYPTED_CONTENT_RETRIES,
        )
        self.assertGreater(CODEX_ENCRYPTED_CONTENT_RETRIES, 0)
        proxy.return_value.start.assert_called_once_with()


    def test_wire_proxy_shutdown_redacts_sensitive_body_fields(self) -> None:
        harness = CodexHarness(CodexConfig(), 0)
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "wire.jsonl"
            log_path.write_text(
                json.dumps({"body": {"api_key": "secret", "input": "keep"}})
                + "\n",
                encoding="utf-8",
            )
            proxy = MagicMock(log_path=log_path, recording_error=None, responses_tools_path=None)
            harness._wire_proxy = proxy

            harness._stop_wire_proxy()

            entry = json.loads(log_path.read_text(encoding="utf-8"))
        proxy.stop.assert_called_once_with()
        self.assertEqual(entry["body"]["api_key"], "<redacted>")
        self.assertEqual(entry["body"]["input"], "keep")
        self.assertIsNone(harness._wire_proxy)

    def test_redacts_api_base_credentials_and_query(self) -> None:
        self.assertEqual(
            redact_api_base("https://user:secret@example.test:8443/v1?api_key=x"),
            "https://example.test:8443/v1",
        )


if __name__ == "__main__":
    unittest.main()
