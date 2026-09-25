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
        self.assertEqual(trajectory["tool_schema_source"], "wire")
        self.assertEqual(len(actf_to_sft(trajectory)["tools"]), 2)
        self.assertIsNot(trajectory["tools"], tools)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "native.jsonl"
            source.write_text("".join(json.dumps(row) + "\n" for row in _events()))
            actual = convert_codex_trace(source, Path(tmp) / "actf.json", tools=tools)
            self.assertEqual(actual["tools"], tools)

    def test_missing_schema_is_explicitly_inferred_not_claimed_native(self):
        self.assertEqual(codex_events_to_actf(_events())["tool_schema_source"], "inferred_from_calls")
        trajectory = codex_events_to_actf(_events(), tools=[])
        self.assertEqual(trajectory["tools"], [])
        with self.assertRaisesRegex(SFTConversionError, "missing schema"):
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
