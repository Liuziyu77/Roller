from __future__ import annotations

from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from roller.codex_actf import codex_events_to_actf, convert_codex_trace
from roller.codex_tools import read_tool_capture, tool_snapshot
from roller.collector import Collector
from roller.harnesses.codex import CodexConfig, CodexHarness
from roller.sft import SFTConversionError, actf_to_sft
from roller.wire_trace import WireTraceProxy, _ProxyHandler


TOOLS = [
    {
        "type": "function", "name": "exec_command", "description": "Execute exactly this command.",
        "strict": True,
        "parameters": {
            "type": "object", "required": ["cmd", "token"], "additionalProperties": False,
            "properties": {
                "cmd": {"type": "string", "description": "Shell command."},
                "token": {"anyOf": [{"$ref": "#/$defs/mode"}, {"type": "null"}]},
            },
            "$defs": {"mode": {"type": "string", "enum": ["one", "two"]}},
        },
    },
    {"type": "function", "name": "never_called", "description": "Available but unused.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "namespace", "name": "agents", "description": "Agent tools.", "tools": [
        {"type": "function", "name": "wait", "parameters": {"type": "object", "properties": {}}},
    ]},
    {"type": "custom", "name": "apply_patch", "description": "Apply a patch.",
     "format": {"type": "grammar", "syntax": "lark", "definition": 'start: "patch"'}},
    {"type": "web_search", "search_context_size": "low"},
]


def events(call: dict | None = None) -> list[dict]:
    payloads = [{"type": "message", "role": "user", "content": "Work."}]
    if call is not None:
        payloads.extend([call, {
            "type": "custom_tool_call_output" if call["type"] == "custom_tool_call" else "function_call_output",
            "call_id": call["call_id"], "output": "Done.",
        }])
    payloads.append({"type": "message", "role": "assistant", "content": "Finished."})
    return [{"type": "response_item", "payload": p} for p in payloads]


class CodexToolsTests(unittest.TestCase):
    def convert(self, root: Path, snapshots: list[dict], call: dict | None = None) -> dict:
        source = root / "trace.jsonl"
        source.write_text("\n".join(json.dumps(e) for e in events(call)) + "\n")
        capture = root / "tools_capture.jsonl"
        capture.write_text("".join(json.dumps({"kind": "codex_tools", "seq": n, "snapshot": s}) + "\n"
                                   for n, s in enumerate(snapshots, 1)))
        return convert_codex_trace(source, root / "trace_ACTF.json", tools_trace=capture)

    def test_full_definitions_including_unused_namespace_custom_and_builtin(self) -> None:
        call = {"type": "function_call", "call_id": "c1", "name": "exec_command",
                "arguments": '{"cmd":"true","token":null}'}
        with tempfile.TemporaryDirectory() as tmp:
            trajectory = self.convert(Path(tmp), [tool_snapshot({"tools": TOOLS})] * 2, call)
        record = actf_to_sft(trajectory)
        self.assertEqual(record["tools"], TOOLS)
        self.assertEqual(trajectory["tools"], TOOLS)
        self.assertEqual(len(trajectory["tools_capture"]["requests"]), 2)
        self.assertEqual(record["messages"][2]["tool_calls"][0]["function"]["arguments"], call["arguments"])
        record["tools"][0]["parameters"]["required"].clear()
        self.assertEqual(trajectory["tools"], TOOLS)

    def test_namespace_calls_preserve_original_name_and_namespace(self) -> None:
        for name, namespace in [("wait", "agents"), ("agents.wait", None)]:
            with self.subTest(name=name):
                call = {"type": "function_call", "call_id": "c", "name": name, "arguments": "{}"}
                if namespace:
                    call["namespace"] = namespace
                record = actf_to_sft(codex_events_to_actf(events(call), tools=TOOLS))
                actual = record["messages"][2]["tool_calls"][0]["function"]
                self.assertEqual(actual["name"], name)
                self.assertEqual(actual.get("namespace"), namespace)

    def test_custom_tool_input_is_not_invented_json_schema_or_arguments(self) -> None:
        call = {"type": "custom_tool_call", "call_id": "patch", "name": "apply_patch", "input": "patch\nraw text"}
        record = actf_to_sft(codex_events_to_actf(events(call), tools=TOOLS))
        self.assertEqual(record["messages"][2]["tool_calls"], [{
            "id": "patch", "type": "custom", "custom": {"name": "apply_patch", "input": "patch\nraw text"},
        }])
        self.assertEqual(record["tools"], TOOLS)

    def test_missing_capture_never_infers_even_without_calls(self) -> None:
        for call in [None, {"type": "function_call", "call_id": "c", "name": "invented", "arguments": "{}"}]:
            trajectory = codex_events_to_actf(events(call))
            self.assertIsNone(trajectory["tools"])
            with self.assertRaisesRegex(SFTConversionError, "not captured"):
                actf_to_sft(trajectory)
        self.assertEqual(read_tool_capture(None)["status"], "missing")

    def test_actual_empty_or_omitted_tools_is_distinct_from_missing_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for snapshot in [tool_snapshot({"tools": []}), tool_snapshot({})]:
                record = actf_to_sft(self.convert(Path(tmp), [snapshot]))
                self.assertEqual(record["tools"], [])

    def test_changing_schemas_are_retained_and_not_silently_unioned(self) -> None:
        modified = deepcopy(TOOLS)
        modified[0]["description"] = "New definition."
        snapshots = [tool_snapshot({"tools": TOOLS}), tool_snapshot({"tools": modified})]
        with tempfile.TemporaryDirectory() as tmp:
            trajectory = self.convert(Path(tmp), snapshots)
        self.assertEqual(trajectory["tools_capture"]["status"], "changed")
        self.assertEqual([r["snapshot"] for r in trajectory["tools_capture"]["requests"]], snapshots)
        with self.assertRaisesRegex(SFTConversionError, "changed across requests"):
            actf_to_sft(trajectory)

    def test_programmatic_tools_are_retained_without_fake_function_schema(self) -> None:
        item = {"type": "additional_tools", "tools": [{"name": "programmatic", "schema": "native text"}]}
        snapshot = tool_snapshot({"tools": TOOLS, "input": [item, {"role": "user", "content": "private"}]})
        with tempfile.TemporaryDirectory() as tmp:
            trajectory = self.convert(Path(tmp), [snapshot])
        self.assertEqual(snapshot["additional_tools"], [item])
        self.assertNotIn("private", json.dumps(snapshot))
        with self.assertRaisesRegex(SFTConversionError, "additional_tools"):
            actf_to_sft(trajectory)

    def test_unmatched_call_fails_without_adding_a_definition(self) -> None:
        call = {"type": "function_call", "call_id": "c", "name": "unseen", "arguments": "{}"}
        with self.assertRaisesRegex(SFTConversionError, "missing captured schema"):
            actf_to_sft(codex_events_to_actf(events(call), tools=TOOLS))

    def test_offline_full_wire_reader_and_invalid_captures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wire.jsonl"
            path.write_text(json.dumps({"seq": 1, "path": "/v1/responses", "body": {"tools": TOOLS}}) + "\n")
            capture = read_tool_capture(path)
            self.assertEqual(capture["status"], "captured")
            self.assertEqual(capture["requests"][0]["snapshot"]["tools"], TOOLS)
            for tail in ['{', json.dumps({"kind": "codex_tools_error", "error": "disk failed"})]:
                with path.open("a") as stream:
                    stream.write(tail + "\n")
                self.assertEqual(read_tool_capture(path)["status"], "invalid")

    def test_capture_survives_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "tools_capture.jsonl").write_text("capture\n")
            (path / "wire_trace.jsonl").write_text("wire\n")
            Collector._prune_sample(path)
            self.assertTrue((path / "tools_capture.jsonl").is_file())
            self.assertFalse((path / "wire_trace.jsonl").exists())


class CodexToolsProxyTests(unittest.TestCase):
    def test_capture_transport_requirements(self) -> None:
        with self.assertRaisesRegex(ValueError, "network host"):
            CodexConfig(api_base="http://example.test/v1", network="bridge")
        with self.assertRaisesRegex(ValueError, "no-codex-websockets"):
            CodexConfig(api_base="http://example.test/v1", wire_trace_mode="full", supports_websockets=True)
        # Transport restrictions apply only when an HTTP capture proxy is used.
        CodexConfig(api_base="http://example.test/v1", wire_trace_mode="off", network="bridge")

    def test_unparseable_request_marks_capture_invalid_but_is_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proxy = WireTraceProxy("http://example.test/v1", root / "wire.jsonl", responses_tools_path=root / "tools.jsonl")
            handler = MagicMock()
            handler.server.proxy = proxy
            handler.headers = {"Content-Length": "2"}
            handler.rfile = io.BytesIO(b"[]")
            handler.path = "/v1/responses"
            handler.command = "POST"
            handler._record.side_effect = lambda data: _ProxyHandler._record(handler, data)
            response = MagicMock(status=400, headers={})
            response.read.return_value = b""
            with patch("roller.wire_trace.urllib_request.urlopen", return_value=response) as send:
                _ProxyHandler._handle(handler)
            self.assertEqual(send.call_args.args[0].data, b"[]")
            self.assertIn("non-object", proxy.recording_error)

    def test_default_actf_captures_every_request_without_mutating_forwarded_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = CodexHarness(CodexConfig(api_base="http://example.test/v1"), 0)
            with patch.object(WireTraceProxy, "start"), patch.object(WireTraceProxy, "stop"):
                harness._start_wire_proxy(root)
                proxy = harness._wire_proxy
                self.assertIsNotNone(proxy)
                bodies = [
                    {"model": "m", "tools": TOOLS, "tool_choice": "auto", "parallel_tool_calls": False,
                     "input": [{"role": "user", "content": "private prompt"}]},
                    {"model": "m", "tools": TOOLS},
                    {"model": "m", "tools": []},
                ]
                for body in bodies:
                    raw = json.dumps(body).encode()
                    handler = MagicMock()
                    handler.server.proxy = proxy
                    handler.headers = {"Content-Length": str(len(raw)), "Content-Type": "application/json", "Authorization": "Bearer secret"}
                    handler.rfile = io.BytesIO(raw)
                    handler.path = "/v1/responses"
                    handler.command = "POST"
                    handler._record.side_effect = lambda data: _ProxyHandler._record(handler, data)
                    response = MagicMock(status=200, headers={})
                    response.read.return_value = b""
                    with patch("roller.wire_trace.urllib_request.urlopen", return_value=response) as send:
                        _ProxyHandler._handle(handler)
                    self.assertEqual(send.call_args.args[0].data, raw)
                harness._stop_wire_proxy()
            raw_capture = (root / "tools_capture.jsonl").read_text()
            captures = [json.loads(line) for line in raw_capture.splitlines()]
            self.assertEqual([r["snapshot"] for r in captures], [tool_snapshot(b) for b in bodies])
            self.assertNotIn("private prompt", raw_capture)
            self.assertNotIn("Bearer secret", raw_capture)
            # Shutdown redaction must not destroy legitimate schema properties named token.
            self.assertEqual(captures[0]["snapshot"]["tools"][0]["parameters"]["properties"]["token"],
                             TOOLS[0]["parameters"]["properties"]["token"])

    def test_capture_disabled_does_not_reuse_previous_attempt(self) -> None:
        for config in [CodexConfig(), CodexConfig(wire_trace_mode="off"),
                       CodexConfig(api_base="http://example.test/v1", supports_websockets=True)]:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "tools_capture.jsonl"
                path.write_text("stale")
                harness = CodexHarness(config, 0)
                harness._start_wire_proxy(Path(tmp))
                self.assertIsNone(harness._wire_proxy)
                self.assertFalse(path.exists())

    def test_recording_failure_invalidates_capture_but_preserves_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proxy = WireTraceProxy("http://example.test/v1", root / "wire.jsonl", responses_tools_path=root / "tools.jsonl")
            proxy.write_tools({"kind": "codex_tools", "seq": 1, "snapshot": {"tools": TOOLS}})
            proxy.recording_error = "disk failure"
            harness = CodexHarness(CodexConfig(), 0)
            harness._wire_proxy = proxy
            with patch.object(proxy, "stop"):
                harness._stop_wire_proxy()
            capture = read_tool_capture(root / "tools.jsonl")
            self.assertEqual(capture["status"], "invalid")
            self.assertEqual(len(capture["requests"]), 1)


if __name__ == "__main__":
    unittest.main()
