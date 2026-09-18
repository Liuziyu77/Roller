from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from roller.claude_code_actf import (
    claude_code_events_to_actf,
    convert_claude_code_trace,
    extract_anthropic_wire_context,
)
from roller.sft import actf_to_sft
from roller.usage import extract_claude_code_usage


def _events() -> list[dict]:
    return [
        {
            "type": "user",
            "uuid": "user-1",
            "timestamp": "2026-08-22T01:00:00Z",
            "message": {"role": "user", "content": "inspect input.txt"},
        },
        {
            "type": "assistant",
            "uuid": "assistant-1",
            "timestamp": "2026-08-22T01:00:01Z",
            "message": {
                "id": "msg-1",
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [
                    {"type": "thinking", "thinking": "I should read it."},
                    {
                        "type": "tool_use",
                        "id": "toolu-1",
                        "name": "Read",
                        "input": {"file_path": "/roller/workspace/input.txt"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 10,
                    "cache_read_input_tokens": 20,
                    "output_tokens": 8,
                },
            },
        },
        {
            "type": "user",
            "uuid": "tool-result-1",
            "timestamp": "2026-08-22T01:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu-1",
                        "content": "hello\n",
                    }
                ],
            },
            "toolUseResult": {"type": "text", "filePath": "input.txt"},
        },
        {
            "type": "assistant",
            "uuid": "assistant-2",
            "timestamp": "2026-08-22T01:00:03Z",
            "message": {
                "id": "msg-2",
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": "The file contains hello."}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 3,
                    "cache_creation_input_tokens": 4,
                    "cache_read_input_tokens": 30,
                    "output_tokens": 6,
                },
            },
        },
    ]


class ClaudeCodeActfTests(unittest.TestCase):
    def test_signed_thinking_preserves_only_native_text_and_does_not_mutate_raw(self) -> None:
        for native in ("", "Visible summary."):
            with self.subTest(native=native):
                events = _events()
                events[1]["message"]["content"] = [
                    {"type": "thinking", "thinking": native, "signature": "opaque-signature"},
                    {"type": "redacted_thinking", "data": "opaque-ciphertext"},
                    {"type": "text", "text": "answer"},
                ]
                before = json.dumps(events)
                trajectory = claude_code_events_to_actf(events)
                record = actf_to_sft(trajectory)
                assistant = next(m for m in record["messages"] if m["role"] == "assistant")
                self.assertEqual(assistant["reasoning_content"], native)
                self.assertNotIn("opaque-", json.dumps(record))
                self.assertEqual(json.dumps(events), before)


    def test_converts_thinking_tools_results_usage_and_sft(self) -> None:
        trajectory = claude_code_events_to_actf(
            _events(), user_messages=["inspect input.txt"]
        )

        self.assertEqual(trajectory["schema_version"], "ACTF_v1.0")
        self.assertEqual(len(trajectory["steps"]), 2)
        first, second = trajectory["steps"]
        self.assertEqual(first["user_content"], "inspect input.txt")
        self.assertEqual(
            first["assistant_content"]["reasoning_content"], "I should read it."
        )
        self.assertEqual(first["assistant_content"]["tool_calls"][0]["id"], "toolu-1")
        self.assertEqual(first["observation"][0]["content"][0]["text"], "hello\n")
        self.assertEqual(first["metric"]["prompt_tokens_len"], 32)
        self.assertEqual(second["assistant_content"]["content"], "The file contains hello.")
        self.assertEqual(trajectory["tools"][0]["function"]["name"], "Read")

        sft = actf_to_sft(trajectory)
        self.assertEqual(sft["messages"][1], {"role": "user", "content": "inspect input.txt"})
        self.assertEqual(sft["messages"][2]["reasoning_content"], "I should read it.")
        self.assertEqual(sft["messages"][3]["role"], "tool")

    def test_extracts_anthropic_wire_system_and_tool_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            wire = Path(temporary) / "wire.jsonl"
            wire.write_text(
                json.dumps(
                    {
                        "kind": "tools",
                        "params": {
                            "system": [{"type": "text", "text": "You are Claude Code."}],
                            "max_tokens": 32000,
                        },
                        "tools": [
                            {
                                "name": "Read",
                                "description": "Read a file",
                                "input_schema": {
                                    "type": "object",
                                    "properties": {"file_path": {"type": "string"}},
                                    "required": ["file_path"],
                                },
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            system, tools = extract_anthropic_wire_context(wire)
        self.assertEqual(system, "You are Claude Code.")
        assert tools is not None
        self.assertEqual(tools[0]["type"], "function")
        self.assertEqual(tools[0]["function"]["parameters"]["required"], ["file_path"])

    def test_strip_thinking_and_decode_only_plain_openrouter_reasoning(self) -> None:
        payload = base64.b64encode(
            json.dumps({"type": "reasoning.text", "text": "visible proxy thought"}).encode()
        ).decode()
        events = _events()
        events[1]["message"]["content"] = [
            {"type": "redacted_thinking", "data": f"openrouter.reasoning:{payload}"},
            {"type": "redacted_thinking", "data": "opaque-anthropic-ciphertext"},
            {"type": "text", "text": "answer"},
        ]
        trajectory = claude_code_events_to_actf(
            events, user_messages=["inspect input.txt"]
        )
        self.assertEqual(
            trajectory["steps"][0]["assistant_content"]["reasoning_content"],
            "visible proxy thought",
        )
        stripped = claude_code_events_to_actf(
            events, user_messages=["inspect input.txt"], keep_thinking=False
        )
        self.assertEqual(
            stripped["steps"][0]["assistant_content"]["reasoning_content"], ""
        )

    def test_preserves_image_tool_result_and_exports_multimodal_sft(self) -> None:
        events = _events()
        events[2]["message"]["content"][0]["content"] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "AA==",
                },
            }
        ]
        trajectory = claude_code_events_to_actf(
            events, user_messages=["inspect input.txt"]
        )
        self.assertEqual(
            trajectory["steps"][0]["observation"][0]["content"][0]["type"],
            "image",
        )
        record = actf_to_sft(trajectory)
        self.assertEqual(set(record), {"messages", "tools", "images"})
        tool_message = record["messages"][3]
        self.assertEqual(tool_message["role"], "tool")
        self.assertEqual(tool_message["tool_call_id"], "toolu-1")
        self.assertEqual(tool_message["content"], "<image>")
        self.assertEqual(record["images"], ["data:image/png;base64,AA=="])

    def test_file_conversion_and_usage_deduplicate_streamed_message_ids(self) -> None:
        events = _events()
        duplicate_update = json.loads(json.dumps(events[1]))
        duplicate_update["uuid"] = "assistant-1-update"
        duplicate_update["timestamp"] = "2026-08-22T01:00:01.500Z"
        duplicate_update["message"]["content"] = []
        duplicate_update["message"]["usage"]["output_tokens"] = 9
        events.insert(2, duplicate_update)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "trace.jsonl"
            source.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            stream = Path(temporary) / "stream.jsonl"
            stream.write_text(
                json.dumps({"type": "result", "total_cost_usd": 0.01})
                + "\n"
                + json.dumps({"type": "result", "total_cost_usd": 0.02})
                + "\n",
                encoding="utf-8",
            )
            destination = Path(temporary) / "trace_ACTF.json"
            converted = convert_claude_code_trace(
                source,
                destination,
                user_messages=["inspect input.txt"],
            )
            usage = extract_claude_code_usage(source, stream)
        self.assertEqual(len(converted["steps"]), 2)
        self.assertEqual(usage["request_count"], 2)
        self.assertEqual(usage["input_tokens"], 5)
        self.assertEqual(usage["output_tokens"], 15)
        self.assertEqual(usage["cache_read_tokens"], 50)
        self.assertEqual(usage["cache_write_tokens"], 14)
        self.assertEqual(usage["total_tokens"], 84)
        self.assertEqual(usage["cost_usd"], 0.03)


if __name__ == "__main__":
    unittest.main()
