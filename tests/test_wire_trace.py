from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from roller.wire_trace import (
    REDACTED_VALUE,
    _ProxyHandler,
    WireTraceProxy,
    extract_wire_tools,
    redact_jsonl_file,
    redact_sensitive_values,
    normalize_chat_completion_reasoning_response,
    strip_thinking_from_body,
)


class ForwardPathTests(unittest.TestCase):
    def test_drops_only_scoped_query_parameters(self) -> None:
        proxy = WireTraceProxy(
            "http://example.test",
            Path("/tmp/unused-wire.jsonl"),
            drop_query_parameters=("beta",),
            drop_request_headers=("Anthropic-Beta",),
        )

        self.assertEqual(
            proxy.forward_path("/v1/messages?beta=true&api-version=2026-01-01"),
            "/v1/messages?api-version=2026-01-01",
        )
        self.assertEqual(proxy.forward_path("/v1/models"), "/v1/models")
        self.assertEqual(proxy.drop_request_headers, {"anthropic-beta"})


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


class NormalizeReasoningReplayTests(unittest.TestCase):
    def test_normalizes_stream_and_non_stream_reasoning_fields(self) -> None:
        body = {
            "choices": [
                {"delta": {"reasoning": "stream chain", "content": None}},
                {
                    "message": {
                        "reasoning_text": "final chain",
                        "content": "answer",
                    }
                },
            ]
        }

        normalized = normalize_chat_completion_reasoning_response(body)

        self.assertEqual(
            normalized["choices"][0]["delta"]["reasoning_content"],
            "stream chain",
        )
        self.assertEqual(
            normalized["choices"][1]["message"]["reasoning_content"],
            "final chain",
        )
        self.assertNotIn("reasoning", normalized["choices"][0]["delta"])
        self.assertNotIn("reasoning_text", normalized["choices"][1]["message"])

    def test_preserves_existing_reasoning_content_and_unrelated_fields(self) -> None:
        body = {
            "id": "response",
            "choices": [
                {
                    "delta": {
                        "reasoning_content": "native",
                        "reasoning": "fallback",
                    }
                }
            ],
        }

        normalized = normalize_chat_completion_reasoning_response(body)

        self.assertEqual(normalized["id"], "response")
        self.assertEqual(
            normalized["choices"][0]["delta"]["reasoning_content"], "native"
        )
        self.assertNotIn("reasoning", normalized["choices"][0]["delta"])

    def test_normalizes_sse_data_line(self) -> None:
        line = b'data: {"choices":[{"delta":{"reasoning":"chain"}}]}\n'

        normalized = _ProxyHandler._normalize_sse_reasoning_line(line)

        payload = json.loads(normalized.removeprefix(b"data: "))
        self.assertEqual(
            payload["choices"][0]["delta"]["reasoning_content"], "chain"
        )

    def test_client_disconnect_during_stream_write_is_not_raised(self) -> None:
        class DisconnectedWriter:
            def write(self, payload: bytes) -> None:
                del payload
                raise BrokenPipeError

            def flush(self) -> None:
                raise AssertionError("flush must not run after a failed write")

        handler = object.__new__(_ProxyHandler)
        handler.wfile = DisconnectedWriter()
        handler.close_connection = False

        self.assertFalse(handler._write_client(b"chunk"))
        self.assertTrue(handler.close_connection)


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
