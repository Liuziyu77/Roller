import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from roller.wire_trace import WireTraceProxy, _ProxyHandler, extract_wire_tools


class WireToolSourceTests(unittest.TestCase):
    def body(self):
        return {
            "tools": [{"type": "function", "name": "read", "description": "a\u2028b"}],
            "input": [
                {"type": "tool_search_output", "additional_tools": [
                    {"type": "namespace", "name": "functions", "tools": [
                        {"type": "function", "name": "write", "parameters": {"type": "object"}}
                    ]}
                ]},
                {"type": "message", "content": "do not collect arbitrary tools"},
            ],
        }

    def test_reads_additional_tools_without_loading_whole_wire_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wire.jsonl"
            path.write_text(json.dumps({"kind": "request", "body": self.body()}, ensure_ascii=False) + "\n")
            with patch.object(Path, "read_text", side_effect=AssertionError("whole file read")):
                tools = extract_wire_tools(path)
            self.assertEqual([tool["name"] for tool in tools], ["read", "functions"])
            self.assertEqual(tools[0]["description"], "a\u2028b")

    def test_compact_logging_keeps_additional_tools_and_source_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wire.jsonl"
            proxy = WireTraceProxy("http://example.test/v1", path, mode="tools")
            handler = MagicMock()
            handler.server.proxy = proxy
            handler.command, handler.path, handler.headers = "POST", "/v1/responses", {}
            raw = json.dumps(self.body()).encode()
            _ProxyHandler._record(handler, raw)
            _ProxyHandler._record(handler, raw)
            with path.open() as stream:
                events = [json.loads(line) for line in stream]
            for event in events:
                self.assertEqual([g["path"] for g in event["tool_schema_sources"]],
                                 ["tools", "input[0].additional_tools"])
                self.assertNotIn("body", event)
            self.assertEqual([tool["name"] for tool in extract_wire_tools(path)], ["read", "functions"])

    def test_does_not_extract_declarations_from_arbitrary_nested_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wire.jsonl"
            body = {"input": [{"content": {"additional_tools": [{"name": "not_a_declaration"}]}}]}
            path.write_text(json.dumps({"kind": "request", "body": body}))
            self.assertIsNone(extract_wire_tools(path))
