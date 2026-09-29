from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from roller.codex_actf import codex_events_to_actf, convert_codex_trace
from roller.sft import actf_to_sft, SFTConversionError
from test_codex_actf import _events


class CodexIntegrityTests(unittest.TestCase):
    def test_captured_schema_used_even_for_unused_tools(self):
        tools = [{"type": "function", "name": name, "description": "native description",
                  "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}
                 for name in ("exec_command", "unused_tool")]
        trajectory = codex_events_to_actf(_events(), tools=tools)
        self.assertEqual(trajectory["tools"], tools)
        self.assertEqual(trajectory["tools_format"], "responses")
        self.assertEqual(trajectory["tools_capture"], {"source": "provided", "status": "captured"})
        self.assertEqual(len(actf_to_sft(trajectory)["tools"]), 2)
        self.assertIsNot(trajectory["tools"], tools)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "native.jsonl"
            source.write_text("".join(json.dumps(row) + "\n" for row in _events()))
            capture = Path(tmp) / "tools_capture.jsonl"
            capture.write_text(json.dumps({"kind": "codex_tools", "seq": 1, "snapshot": {"tools": tools}}) + "\n" +
                               json.dumps({"kind": "codex_tools_complete", "request_count": 1}) + "\n")
            actual = convert_codex_trace(source, Path(tmp) / "actf.json", tools_trace=capture)
            self.assertEqual(actual["tools"], tools)
            self.assertEqual(actual["tools_capture"]["source"], "responses_request")

    def test_missing_schema_follows_upstream_no_inference(self):
        missing = codex_events_to_actf(_events())
        self.assertIsNone(missing["tools"])
        self.assertEqual(missing["tools_capture"]["status"], "missing")
        with self.assertRaisesRegex(SFTConversionError, "not captured"):
            actf_to_sft(missing)
        trajectory = codex_events_to_actf(_events(), tools=[])
        self.assertEqual(trajectory["tools"], [])
        with self.assertRaisesRegex(SFTConversionError, "missing captured schema"):
            actf_to_sft(trajectory)

    def test_synthetic_missing_result_remains_diagnostic_only(self):
        events = _events()
        events.pop(6)
        trajectory = codex_events_to_actf(events)
        self.assertFalse(trajectory["projection"]["complete"])
        self.assertEqual(trajectory["projection"]["missing_tool_call_ids"], ["call-1"])
        with self.assertRaisesRegex(SFTConversionError, "incomplete diagnostic"):
            actf_to_sft(trajectory)

    def test_orphan_repeated_call_and_repeated_result_rejected(self):
        for case in ("orphan", "call", "result"):
            events = _events()
            if case == "orphan": events[6]["payload"]["call_id"] = "unknown"
            elif case == "call": events.insert(5, deepcopy(events[4]))
            else: events.insert(7, deepcopy(events[6]))
            with self.subTest(case=case), self.assertRaises(ValueError):
                codex_events_to_actf(events)
