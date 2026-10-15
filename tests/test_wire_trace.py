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
from roller.capture import CallRecorder, read_jsonl


class FullCallCaptureTests(unittest.TestCase):
    def forward(self, root, recorder, payload, *, chat_session=None, native_run=None):
        body=json.dumps({'model':'model','messages':[{'role':'user','content':'hello'}]}).encode()
        handler=MagicMock()
        handler.server.proxy=WireTraceProxy('http://model.test',root/'wire.jsonl',mode='full',capture_recorder=recorder,
            capture_identity_header='x-roller-native-session-id' if chat_session else None,
            capture_run_header='x-roller-native-run-id' if native_run else None)
        handler.headers={'Content-Length':str(len(body)),'Content-Type':'application/json','Authorization':'Bearer private-credential'}
        handler.rfile=io.BytesIO(body)
        handler.wfile=io.BytesIO()
        handler.path='/v1/messages'
        if chat_session:
            handler.path='/v1/chat/completions'
            handler.headers['x-roller-native-session-id']=chat_session
        if native_run:
            handler.headers['x-roller-native-run-id']=native_run
        handler.command='POST'
        handler._record.side_effect=lambda b:_ProxyHandler._record(handler,b)
        response=MagicMock(status=200,headers={'Content-Type':'application/json'})
        stream=io.BytesIO(payload)
        response.read1.side_effect=stream.read1
        with patch('roller.wire_trace.urllib_request.urlopen',return_value=response) as send:
            _ProxyHandler._handle(handler)
            if chat_session:
                self.assertNotIn('x-roller-native-session-id',dict((k.lower(),v) for k,v in send.call_args.args[0].header_items()))
            if native_run:
                self.assertNotIn('x-roller-native-run-id',dict((k.lower(),v) for k,v in send.call_args.args[0].header_items()))
        return handler.wfile.getvalue()

    def test_full_response_is_captured_without_changing_delivery(self):
        payload=b'{"type":"message","id":"m","content":[{"type":"text","text":"hello"}]}'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);recorder=CallRecorder(root/'capture/attempt-0001')
            self.assertEqual(self.forward(root,recorder,payload),payload)
            self.assertEqual(recorder.response_path(1).read_bytes(),payload)
            events=read_jsonl(recorder.events)
            self.assertTrue(events[-1]['complete'])
            self.assertNotIn('private-credential',recorder.events.read_text())

    def test_capture_storage_failure_does_not_interrupt_model_response(self):
        payload=b'{"type":"message","id":"m","content":[]}'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);recorder=CallRecorder(root/'capture/attempt-0001')
            with patch.object(recorder,'open_response',side_effect=OSError('disk full')):
                self.assertEqual(self.forward(root,recorder,payload),payload)
            self.assertFalse(read_jsonl(recorder.events)[-1]['complete'])
            self.assertEqual(recorder.error,'disk full')

    def test_chat_session_is_recorded_locally_and_not_sent_to_provider(self):
        payload=b'{"id":"m","choices":[]}'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);recorder=CallRecorder(root/'capture/attempt-0001')
            self.assertEqual(self.forward(root,recorder,payload,chat_session='child-session'),payload)
            events=read_jsonl(recorder.events)
            self.assertEqual(events[0]['native_identity'],{'session_id':'child-session'})
            self.assertEqual(recorder.response_path(1).read_bytes(),payload)

    def test_native_notification_run_is_local_and_not_sent_to_provider(self):
        payload=b'{"id":"m","choices":[]}'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);recorder=CallRecorder(root/'capture/attempt-0001')
            self.assertEqual(self.forward(root,recorder,payload,chat_session='root',native_run='announce:child'),payload)
            self.assertEqual(read_jsonl(recorder.events)[0]['native_identity'],{'session_id':'root','run_id':'announce:child'})


class ForwardPathTests(unittest.TestCase):
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
