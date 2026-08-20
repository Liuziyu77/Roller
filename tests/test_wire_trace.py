from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from roller.wire_trace import (
    REDACTED_VALUE,
    extract_wire_tools,
    redact_jsonl_file,
    redact_sensitive_values,
    strip_thinking_from_body,
)


class StripThinkingTests(unittest.TestCase):
    def test_strips_reasoning_from_history_assistant_messages(self) -> None:
        body = {
            "model": "m",
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "q"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "secret"},
                        {"type": "text", "text": "answer"},
                    ],
                    "reasoning_content": "chain",
                },
                {"role": "user", "content": "q2"},
                {
                    "role": "assistant",
                    "content": "answer2",
                    "reasoning": "chain2",
                    "reasoning_text": "chain3",
                },
            ],
            "tools": [],
        }

        stripped = strip_thinking_from_body(body)

        self.assertEqual(
            stripped["messages"][2]["content"], [{"type": "text", "text": "answer"}]
        )
        self.assertNotIn("reasoning_content", stripped["messages"][2])
        self.assertNotIn("reasoning", stripped["messages"][4])
        self.assertNotIn("reasoning_text", stripped["messages"][4])
        # Non-assistant messages and top-level keys stay untouched.
        self.assertEqual(stripped["messages"][0]["content"], "sys")
        self.assertEqual(stripped["model"], "m")

    def test_non_object_body_passes_through(self) -> None:
        body = [{"role": "assistant", "reasoning_content": "keep"}]
        self.assertIs(strip_thinking_from_body(body), body)
        self.assertEqual(body[0]["reasoning_content"], "keep")

    def test_strips_reasoning_items_from_responses_input(self) -> None:
        body = {
            "model": "m",
            "input": [
                {"role": "developer", "content": "sys"},
                {"role": "user", "content": [{"type": "input_text", "text": "q"}]},
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "encrypted_content": "opaque",
                    "summary": [{"type": "summary_text", "text": "secret"}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "answer"}],
                },
            ],
        }

        stripped = strip_thinking_from_body(body)

        self.assertEqual(
            [item.get("type") for item in stripped["input"]],
            [None, None, "message"],
        )


class ExtractWireToolsTests(unittest.TestCase):
    def test_extracts_from_tools_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wire.jsonl"
            entries = [
                {"kind": "request", "seq": 1, "params": {"stream": True}},
                {
                    "kind": "tools",
                    "seq": 2,
                    "tools": [{"type": "function", "function": {"name": "read"}}],
                },
            ]
            path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
            tools = extract_wire_tools(path)
        self.assertEqual(len(tools or []), 1)
        self.assertEqual(tools[0]["function"]["name"], "read")

    def test_extracts_from_full_body_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wire.jsonl"
            entry = {
                "kind": "request",
                "body": {"tools": [{"type": "function", "function": {"name": "exec"}}]},
            }
            path.write_text(json.dumps(entry), encoding="utf-8")
            tools = extract_wire_tools(path)
        self.assertEqual(tools[0]["function"]["name"], "exec")

    def test_returns_none_when_missing_or_unreadable(self) -> None:
        self.assertIsNone(extract_wire_tools(Path("/nonexistent/wire.jsonl")))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wire.jsonl"
            path.write_text(json.dumps({"kind": "request", "params": {}}), encoding="utf-8")
            self.assertIsNone(extract_wire_tools(path))

    def test_skips_non_object_full_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wire.jsonl"
            entries = [
                {"kind": "request", "body": ["not", "an", "object"]},
                {
                    "kind": "request",
                    "body": {
                        "tools": [{"type": "function", "function": {"name": "read"}}]
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
            tools = extract_wire_tools(path)
        self.assertEqual(tools[0]["function"]["name"], "read")


class TraceRedactionTests(unittest.TestCase):
    def test_redacts_nested_credentials_without_mutating_input(self) -> None:
        original = {
            "options": {
                "apiKey": "secret-key",
                "getApiKey": "[Function]",
                "access_token": "secret-token",
                "totalTokens": 17,
            },
            "messages": [{"content": "apiKey is ordinary text here"}],
        }

        redacted = redact_sensitive_values(original)

        self.assertEqual(redacted["options"]["apiKey"], REDACTED_VALUE)
        self.assertEqual(redacted["options"]["getApiKey"], REDACTED_VALUE)
        self.assertEqual(redacted["options"]["access_token"], REDACTED_VALUE)
        self.assertEqual(redacted["options"]["totalTokens"], 17)
        self.assertEqual(redacted["messages"], original["messages"])
        self.assertEqual(original["options"]["apiKey"], "secret-key")

    def test_redacts_jsonl_atomically_and_preserves_malformed_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.jsonl"
            path.write_text(
                json.dumps({"options": {"apiKey": "secret", "maxTokens": 20}})
                + "\nnot-json\n",
                encoding="utf-8",
            )

            redact_jsonl_file(path)

            lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[0])["options"]["apiKey"], REDACTED_VALUE)
        self.assertEqual(json.loads(lines[0])["options"]["maxTokens"], 20)
        self.assertEqual(lines[1], "not-json")


if __name__ == "__main__":
    unittest.main()
