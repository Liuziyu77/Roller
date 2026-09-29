"""Interactions between native tool capture and the retained generic fixes."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from roller.codex_actf import convert_codex_trace
from roller.codex_tools import read_tool_capture, tool_snapshot
from roller.harnesses.codex import CodexConfig, CodexHarness
from roller.sft import SFTConversionError, actf_to_sft, _normalize_tool_call
from roller.wire_trace import WireTraceProxy, _ProxyHandler
from test_codex_tools import TOOLS, events


class UpstreamMergeTests(unittest.TestCase):
    def test_capture_and_detail_switches_are_independent(self):
        for mode in ("off", "actf", "full"):
            for detail in (None, "auto"):
                with self.subTest(mode=mode, detail=detail), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    config = CodexConfig(api_base="http://example.test/v1", wire_trace_mode=mode,
                                         responses_image_detail=detail)
                    harness = CodexHarness(config, 0)
                    with patch.object(WireTraceProxy, "start"), patch.object(WireTraceProxy, "stop"):
                        harness._start_wire_proxy(root)
                        proxy = harness._wire_proxy
                        if mode == "off" and detail is None:
                            self.assertIsNone(proxy)
                            continue
                        self.assertIsNotNone(proxy)
                        self.assertEqual(proxy.mode, {"off": "off", "actf": "tools", "full": "full"}[mode])
                        self.assertEqual(proxy.responses_tools_path is not None, mode != "off")
                        body = {"tools": TOOLS, "input": [{"role": "user", "content": [
                            {"type": "input_image", "image_url": "data:image/png;base64,AA=="},
                        ]}]}
                        original = deepcopy(body)
                        raw = json.dumps(body).encode()
                        handler = MagicMock()
                        handler.server.proxy = proxy
                        handler.headers = {"Content-Length": str(len(raw))}
                        handler.rfile = io.BytesIO(raw)
                        handler.command, handler.path = "POST", "/v1/responses"
                        handler._record.side_effect = lambda data: _ProxyHandler._record(handler, data)
                        response = MagicMock(status=200, headers={})
                        response.read.return_value = b""
                        with patch("roller.wire_trace.urllib_request.urlopen", return_value=response) as send:
                            _ProxyHandler._handle(handler)
                        forwarded = json.loads(send.call_args.args[0].data)
                        expected = deepcopy(body)
                        if detail:
                            expected["input"][0]["content"][0]["detail"] = detail
                        self.assertEqual(forwarded, expected)
                        self.assertEqual(body, original)
                        harness._stop_wire_proxy()
                    self.assertEqual((root / "wire_trace.jsonl").exists(), mode != "off")
                    capture_path = root / "tools_capture.jsonl"
                    self.assertEqual(capture_path.exists(), mode != "off")
                    if mode != "off":
                        capture = read_tool_capture(capture_path)
                        self.assertEqual(capture["status"], "captured")
                        self.assertEqual(capture["requests"][0]["snapshot"], tool_snapshot(forwarded))
                        self.assertNotIn("image_url", capture_path.read_text())

    def test_native_capture_with_images_and_namespaced_custom_input(self):
        tools = [{"type": "namespace", "name": "editing", "tools": [
            {"type": "custom", "name": "patch", "format": {"type": "text"}},
        ]}]
        raw_input = '{"text":"This is a string, not function arguments."}'
        call = {"type": "custom_tool_call", "call_id": "c1", "namespace": "editing",
                "name": "patch", "input": raw_input}
        native = events(call)
        content = [
            {"type": "input_text", "text": '<image name="attached">'},
            {"type": "input_image", "image_url": "data:image/png;base64,AA=="},
            {"type": "input_text", "text": "</image>"},
            {"type": "input_text", "text": "Describe this image."},
        ]
        native[0]["payload"]["content"] = content
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "trace.jsonl"
            source.write_text("".join(json.dumps(e) + "\n" for e in native))
            original = source.read_bytes()
            capture = root / "tools_capture.jsonl"
            capture.write_text(json.dumps({"kind": "codex_tools", "seq": 1, "snapshot": {"tools": tools}}) + "\n" +
                               json.dumps({"kind": "codex_tools_complete", "request_count": 1}) + "\n")
            trajectory = convert_codex_trace(source, root / "actf.json", tools_trace=capture,
                                              user_messages=["Describe this image."])
            record = actf_to_sft(trajectory)
            self.assertEqual(source.read_bytes(), original)
        self.assertEqual(trajectory["steps"][0]["user_content"], content)
        self.assertEqual(record["images"], ["data:image/png;base64,AA=="])
        self.assertEqual(record["tools"], tools)
        custom = next(m for m in record["messages"] if m["role"] == "assistant")["tool_calls"][0]
        self.assertEqual(custom, {"id": "c1", "type": "custom", "custom": {
            "name": "patch", "namespace": "editing", "input": raw_input,
        }})

    def test_legacy_function_namespace_still_flattens_by_default(self):
        call = {"id": "c", "name": "read", "namespace": "files", "arguments": {}}
        original = deepcopy(call)
        self.assertEqual(_normalize_tool_call(call, "call")["function"]["name"], "files.read")
        self.assertEqual(call, original)

    def test_legacy_wire_without_completion_is_diagnostic_not_silently_trusted(self):
        content = [{"type": "input_text", "text": "before"},
                   {"type": "input_image", "image_url": "data:image/png;base64,AA=="},
                   {"type": "input_text", "text": "after"}]
        native = events()
        native[0]["payload"]["content"] = content
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "trace.jsonl"
            source.write_text("".join(json.dumps(e) + "\n" for e in native))
            wire = root / "wire_trace.jsonl"
            wire.write_text(json.dumps({"path": "/v1/responses", "body": {"tools": TOOLS}}) + "\n")
            original = wire.read_bytes()
            trajectory = convert_codex_trace(source, root / "actf.json", tools_trace=wire,
                                              user_messages=["after"])
            self.assertEqual(trajectory["steps"][0]["user_content"], content)
            self.assertEqual(trajectory["tools_capture"]["status"], "invalid")
            with self.assertRaisesRegex(SFTConversionError, "missing completion"):
                actf_to_sft(trajectory)
            self.assertEqual(wire.read_bytes(), original)
