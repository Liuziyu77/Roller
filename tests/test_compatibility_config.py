from copy import deepcopy
from dataclasses import replace
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from roller.cli import _build_parser
from roller.harnesses.codex import CodexConfig, CodexHarness
from roller.harnesses.deepseek import DeepSeekConfig, DeepSeekHarness, RUNNER_PATH
from roller.harnesses.hermes import HermesConfig, HermesHarness
from roller.models import RunRequest, Task
from roller.wire_trace import WireTraceProxy, _ProxyHandler, default_responses_image_detail
from test_deepseek import FakeRuntime


class CompatibilityConfigTests(unittest.TestCase):
    def test_upstream_defaults_preserved_and_overrides_validated(self):
        config = DeepSeekConfig()
        self.assertEqual(config.profile, "sdk-minimal")
        self.assertIsNone(config.permission_mode)
        self.assertIsNone(config.telemetry_disabled)
        self.assertFalse(config.vision)
        self.assertIsNone(CodexConfig().responses_image_detail)
        for settings in ({"profile": "guess"}, {"permission_mode": "guess"}, {"vision": "yes"}):
            with self.assertRaises(ValueError): DeepSeekConfig(**settings)
        with self.assertRaises(ValueError): CodexConfig(responses_image_detail="auto")
        with self.assertRaises(ValueError):
            CodexConfig(api_base="http://example.test", responses_image_detail="auto", supports_websockets=True)

    def test_cli_parses_explicit_overrides(self):
        args = _build_parser().parse_args(["collect", "--tasks", "tasks.jsonl", "--deepseek-profile", "sdk",
                                          "--deepseek-permission-mode", "danger-full-access",
                                          "--deepseek-telemetry-disabled", "--deepseek-vision",
                                          "--codex-image-detail", "auto"])
        self.assertEqual(args.deepseek_profile, "sdk")
        self.assertTrue(args.deepseek_telemetry_disabled)
        self.assertTrue(args.deepseek_vision)
        self.assertEqual(args.codex_image_detail, "auto")

    def test_sdk_runner_keeps_standard_prompt_and_sets_only_explicit_policy(self):
        spec = importlib.util.spec_from_file_location("deepseek_compat_runner", RUNNER_PATH)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        config = {"model": "m", "system_prompt": "minimal prompt", "context_length": 2048, "output_tokens": 128}
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(runner.configure_environment(config), "sdk-minimal")
            self.assertEqual(os.environ["DSH_SYSTEM_PROMPT"], "minimal prompt")
            self.assertNotIn("DSH_PERMISSION_MODE", os.environ)
            self.assertEqual(os.environ["ROLLER_DEEPSEEK_VISION"], "0")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(runner.configure_environment({**config, "profile": "sdk", "permission_mode": "danger-full-access",
                                                          "telemetry_disabled": True, "vision": True}), "sdk")
            self.assertNotIn("DSH_SYSTEM_PROMPT", os.environ)
            self.assertEqual(os.environ["DSH_PERMISSION_MODE"], "danger-full-access")
            self.assertEqual(os.environ["DSH_TELEMETRY_DISABLED"], "1")
            self.assertEqual(os.environ["ROLLER_DEEPSEEK_VISION"], "1")

    def test_sdk_selection_reaches_turn_config_and_metadata(self):
        runtime = FakeRuntime()
        harness = DeepSeekHarness(DeepSeekConfig(profile="sdk", permission_mode="danger-full-access",
                                 telemetry_disabled=True, vision=True, api_key_env="TEST_KEY", wire_trace_mode="off"), 0, runtime)
        from roller.storage import atomic_write_json
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TEST_KEY": "fake"}), \
                patch("roller.harnesses.deepseek.atomic_write_json", wraps=atomic_write_json) as write:
            result = harness.run(RunRequest(0, Task("generic", ("hello",), 0), "model", 60), Path(tmp), 1)
        self.assertEqual(result.status, "ok", result.error)
        turn = write.call_args.args[1]
        self.assertEqual(turn["profile"], "sdk")
        self.assertTrue(turn["vision"])
        self.assertEqual(result.metadata["profile"], "sdk")
        self.assertIsNone(result.metadata["settings"]["tools"])

    def test_detail_is_scoped_and_preserves_present_values(self):
        body = {"input": [{"role": "user", "content": [
            {"type": "input_image", "image_url": "data:image/png;base64,AA=="},
            {"type": "input_image", "detail": "high"},
            {"type": "input_image", "detail": None}]},
            {"type": "function_call_output", "output": {"type": "input_image"}}],
            "metadata": {"type": "input_image"}}
        original = deepcopy(body)
        result = default_responses_image_detail(body, "auto")
        self.assertEqual(body, original)
        self.assertEqual([block["detail"] for block in result["input"][0]["content"]], ["auto", "high", None])
        self.assertNotIn("detail", result["input"][1]["output"])
        self.assertNotIn("detail", result["metadata"])

    def test_detail_proxy_active_with_logging_off_and_only_for_responses(self):
        with tempfile.TemporaryDirectory() as tmp, patch("roller.harnesses.codex.WireTraceProxy") as proxy:
            config = CodexConfig(api_base="http://example.test", wire_trace_mode="off", responses_image_detail="auto")
            CodexHarness(config, 0)._start_wire_proxy(Path(tmp))
            self.assertEqual(proxy.call_args.kwargs["mode"], "off")
            self.assertEqual(proxy.call_args.kwargs["responses_image_detail"], "auto")
        body = {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "x"}]}]}
        for path in ("/v1/responses", "/v1/chat/completions"):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as tmp:
                handler = MagicMock()
                handler.server.proxy = WireTraceProxy("http://example.test", Path(tmp) / "wire.jsonl", mode="off", responses_image_detail="auto")
                encoded = json.dumps(body).encode()
                handler.headers = {"Content-Length": str(len(encoded))}
                handler.rfile, handler.wfile = io.BytesIO(encoded), io.BytesIO()
                handler.path, handler.command = path, "POST"
                response = io.BytesIO(b"{}")
                response.status, response.headers = 200, {}
                with patch("roller.wire_trace.urllib_request.urlopen", return_value=response) as send:
                    _ProxyHandler._handle(handler)
                sent = json.loads(send.call_args.args[0].data)
                self.assertEqual("detail" in sent["input"][0]["content"][0], path.endswith("/responses"))

    def test_unsupported_hermes_attachments_fail_before_container_start(self):
        runtime = MagicMock()
        harness = HermesHarness(HermesConfig(), 0, runtime)
        task = Task("generic", ("hello",), 0, attach_images=("inputs/image.png",))
        with tempfile.TemporaryDirectory() as tmp:
            session = harness.create_session(RunRequest(0, task, "m", 30), Path(tmp), 1)
            with self.assertRaisesRegex(ValueError, "does not support"):
                session.start()
        runtime.run.assert_not_called()
