from __future__ import annotations

import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    def test_explicit_tool_id_repair_works_without_wire_logging(self) -> None:
        for streaming in (False, True):
            with self.subTest(streaming=streaming), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                proxy = WireTraceProxy("http://example.test", root / "wire.jsonl", mode="off", unique_anthropic_tool_ids=True)
                event = ({"type": "content_block_start", "content_block": {"type": "tool_use", "id": "x"}}
                         if streaming else {"type": "message", "content": [{"type": "tool_use", "id": "x"}]})
                ids = []
                for _ in range(2):
                    body = json.dumps(event).encode()
                    response = io.BytesIO(b"data: " + body + b"\n\n" if streaming else body)
                    response.status = 200
                    response.headers = {"Content-Type": "text/event-stream" if streaming else "application/json"}
                    handler = MagicMock()
                    handler.server.proxy = proxy
                    handler.path, handler.command = "/v1/messages", "POST"
                    handler.headers = {"Content-Length": "2"}
                    handler.rfile, handler.wfile = io.BytesIO(b"{}"), io.BytesIO()
                    with patch("roller.wire_trace.urllib_request.urlopen", return_value=response):
                        _ProxyHandler._handle(handler)
                    raw = handler.wfile.getvalue()
                    result = json.loads(raw[6:].strip() if streaming else raw)
                    ids.append(result["content_block"]["id"] if streaming else result["content"][0]["id"])
                self.assertEqual(ids[0], "x")
                self.assertNotEqual(ids[0], ids[1])
                self.assertTrue((root / "tool_id_mappings.jsonl").is_file())
                self.assertFalse((root / "wire.jsonl").exists())

    def test_native_request_is_forwarded_once_without_cot_rewrites(self) -> None:
        body = json.dumps({
            "model": "model",
            "reasoning": {"effort": "high", "summary": "detailed"},
            "input": [{"role": "user", "content": "Inspect the file"}],
            "tools": [{"type": "function", "name": "read", "parameters": {}}],
            "tool_choice": "auto",
        }).encode()
        handler = MagicMock()
        handler.server.proxy = WireTraceProxy("http://example.test/v1", Path("/tmp/unused-wire.jsonl"))
        handler.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
        handler.rfile = io.BytesIO(body)
        handler.path = "/v1/responses"
        handler.command = "POST"
        response = MagicMock(status=200, headers={})
        response.read.return_value = b""
        with patch("roller.wire_trace.urllib_request.urlopen", return_value=response) as send:
            _ProxyHandler._handle(handler)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0].data, body)
        handler._record.assert_called_once_with(body)

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


class ExtractWireToolsTests(unittest.TestCase):
    def test_reads_additional_tool_declarations_without_loading_whole_file(self) -> None:
        tools = [{"type": "function", "name": "read"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wire.jsonl"
            body = {"input": [{"additional_tools": tools},
                              {"content": {"additional_tools": [{"name": "not_a_declaration"}]}}]}
            path.write_text(json.dumps({"kind": "request", "body": body}))
            with patch.object(Path, "read_text", side_effect=AssertionError("whole file read")):
                self.assertEqual(extract_wire_tools(path), tools)

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

    def test_merges_dynamic_tool_schemas_across_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wire.jsonl"
            entries = [
                {
                    "kind": "tools",
                    "tools": [
                        {"type": "function", "function": {"name": "read"}}
                    ],
                },
                {
                    "kind": "tools",
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "read",
                                "description": "latest read schema",
                            },
                        },
                        {
                            "type": "function",
                            "function": {
                                "name": "gateway",
                                "parameters": {"type": "object"},
                            },
                        },
                    ],
                },
            ]
            path.write_text(
                "\n".join(json.dumps(entry) for entry in entries),
                encoding="utf-8",
            )

            tools = extract_wire_tools(path)

        self.assertEqual(
            [tool["function"]["name"] for tool in tools or []],
            ["read", "gateway"],
        )
        assert tools is not None
        self.assertEqual(tools[0]["function"]["description"], "latest read schema")

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
