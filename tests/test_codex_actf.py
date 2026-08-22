from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from roller.codex_actf import (
    _external_mcp_completion,
    codex_events_to_actf,
    convert_codex_trace,
)
from roller.external_thinking import validate_external_thinking_trajectory
from roller.sft import actf_to_sft
from roller.usage import extract_codex_usage


def _events() -> list[dict]:
    return [
        {
            "timestamp": "2026-08-22T01:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "session-1",
                "cli_version": "0.149.0",
                "base_instructions": {"text": "You are Codex."},
            },
        },
        {
            "timestamp": "2026-08-22T01:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "Follow AGENTS.md."}],
            },
        },
        {
            "timestamp": "2026-08-22T01:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "wrapped prompt"}],
            },
        },
        {
            "timestamp": "2026-08-22T01:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Inspect the file."}],
            },
        },
        {
            "timestamp": "2026-08-22T01:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": "call-1",
                "name": "exec_command",
                "arguments": '{"cmd":"sed -n 1,10p input.txt"}',
            },
        },
        {
            "timestamp": "2026-08-22T01:00:05Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 20,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 10,
                        "reasoning_output_tokens": 7,
                        "total_tokens": 110,
                    },
                    "total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 20,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 10,
                        "reasoning_output_tokens": 7,
                        "total_tokens": 110,
                    },
                },
            },
        },
        {
            "timestamp": "2026-08-22T01:00:06Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": json.dumps(
                    {"output": "hello\n", "metadata": {"exit_code": 0}}
                ),
            },
        },
        {
            "timestamp": "2026-08-22T01:00:07Z",
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": [{"text": "The file contains hello."}],
            },
        },
        {
            "timestamp": "2026-08-22T01:00:08Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Done."}],
            },
        },
        {
            "timestamp": "2026-08-22T01:00:09Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 120,
                        "cached_input_tokens": 90,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 8,
                        "reasoning_output_tokens": 4,
                        "total_tokens": 128,
                    },
                    "total_token_usage": {
                        "input_tokens": 220,
                        "cached_input_tokens": 110,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 18,
                        "reasoning_output_tokens": 11,
                        "total_tokens": 238,
                    },
                },
            },
        },
    ]


class CodexActfTests(unittest.TestCase):
    def test_groups_native_events_by_model_request(self) -> None:
        trajectory = codex_events_to_actf(
            _events(), user_messages=["read input.txt"]
        )

        self.assertEqual(trajectory["schema_version"], "ACTF_v1.0")
        self.assertEqual(trajectory["system_prompt"], "You are Codex.\n\nFollow AGENTS.md.")
        self.assertEqual(len(trajectory["steps"]), 2)
        first, second = trajectory["steps"]
        self.assertEqual(first["user_content"], "read input.txt")
        self.assertEqual(
            first["assistant_content"]["reasoning_content"], "Inspect the file."
        )
        self.assertEqual(first["assistant_content"]["tool_calls"][0]["id"], "call-1")
        self.assertEqual(first["observation"][0]["content"][0]["text"], "hello\n")
        self.assertEqual(second["assistant_content"]["content"], "Done.")
        self.assertEqual(
            second["assistant_content"]["reasoning_content"],
            "The file contains hello.",
        )
        self.assertEqual(trajectory["tools"][0]["function"]["name"], "exec_command")

        sft = actf_to_sft(trajectory)
        self.assertEqual(set(sft), {"messages", "tools"})
        self.assertEqual(sft["messages"][1], {"role": "user", "content": "read input.txt"})
        self.assertEqual(sft["messages"][2]["tool_calls"][0]["id"], "call-1")
        self.assertEqual(sft["messages"][3]["role"], "tool")

    def test_can_strip_reasoning_and_extract_cumulative_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "trace.jsonl"
            source.write_text(
                "\n".join(json.dumps(event) for event in _events()) + "\n",
                encoding="utf-8",
            )
            destination = Path(temporary) / "trace_ACTF.json"
            trajectory = convert_codex_trace(
                source,
                destination,
                user_messages=["read input.txt"],
                keep_thinking=False,
            )
            self.assertTrue(destination.is_file())
            self.assertTrue(
                all(
                    step["assistant_content"]["reasoning_content"] == ""
                    for step in trajectory["steps"]
                )
            )
            usage = extract_codex_usage(source)
            self.assertEqual(usage["input_tokens"], 220)
            self.assertEqual(usage["output_tokens"], 18)
            self.assertEqual(usage["cache_read_tokens"], 110)
            self.assertEqual(usage["total_tokens"], 238)
            self.assertEqual(usage["request_count"], 2)

    def test_prefers_visible_reasoning_content_and_filters_codex_context(self) -> None:
        events = _events()
        events.insert(
            2,
            {
                "timestamp": "2026-08-22T01:00:01.500Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "<environment_context><cwd>/work</cwd></environment_context>",
                        }
                    ],
                },
            },
        )
        for event in events:
            payload = event.get("payload", {})
            if payload.get("type") == "reasoning":
                payload["content"] = [
                    {"type": "reasoning_text", "text": "Visible full reasoning."}
                ]
                break

        trajectory = codex_events_to_actf(events, user_messages=["read input.txt"])

        self.assertIn("<environment_context>", trajectory["system_prompt"])
        self.assertEqual(trajectory["steps"][0]["user_content"], "read input.txt")
        self.assertEqual(
            trajectory["steps"][0]["assistant_content"]["reasoning_content"],
            "Visible full reasoning.",
        )

    def test_projects_codex_mcp_think_to_canonical_actf_and_sft_reasoning(self) -> None:
        events = [
            _events()[0],
            {
                "timestamp": "2026-08-22T02:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "solve"}],
                },
            },
            {
                "timestamp": "2026-08-22T02:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "call-think",
                    "name": "exec",
                    "input": (
                        "const r = await tools."
                        "mcp__roller_external_thinking__deep_think"
                        '({thoughts:"Inspect before acting."}); text(r);'
                    ),
                },
            },
            {
                "timestamp": "2026-08-22T02:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "McpToolCall",
                        "id": "exec-inner",
                        "server": "roller_external_thinking",
                        "tool": "deep_think",
                        "arguments": {"thoughts": "Inspect before acting."},
                        "status": "completed",
                        "result": {
                            "content": [{"type": "text", "text": "------"}],
                            "isError": False,
                        },
                    },
                },
            },
            {
                "timestamp": "2026-08-22T02:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-think",
                    "output": '{"content":[{"type":"text","text":"------"}]}',
                },
            },
            {
                "timestamp": "2026-08-22T02:00:04Z",
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {}},
            },
            {
                "timestamp": "2026-08-22T02:00:05Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Done."}],
                },
            },
            {
                "timestamp": "2026-08-22T02:00:06Z",
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {}},
            },
        ]

        trajectory = codex_events_to_actf(events, user_messages=["solve"])

        first_call = trajectory["steps"][0]["assistant_content"]["tool_calls"][0]
        self.assertEqual(first_call["name"], "deep_think")
        self.assertEqual(first_call["arguments"], {"thoughts": "Inspect before acting."})
        self.assertEqual(
            trajectory["steps"][0]["observation"][0]["content"][0]["text"],
            "------",
        )
        stats = validate_external_thinking_trajectory(
            trajectory,
            "external_always",
            require_isolated_deep_think=True,
        )
        self.assertEqual(stats.thought_action_pairs, 1)

        sft = actf_to_sft(trajectory, external_thinking="external_always")
        assistants = [message for message in sft["messages"] if message["role"] == "assistant"]
        self.assertEqual(len(assistants), 1)
        self.assertEqual(assistants[0]["reasoning_content"], "Inspect before acting.")
        self.assertEqual(assistants[0]["content"], "Done.")
        self.assertNotIn(
            "deep_think", [tool["function"]["name"] for tool in sft["tools"]]
        )

    def test_accepts_lower_level_mcp_completion_event_variant(self) -> None:
        normalized = _external_mcp_completion(
            {
                "type": "mcp_tool_call_end",
                "invocation": {
                    "server": "roller_external_thinking",
                    "tool": "deep_think",
                    "arguments": {"thoughts": "check"},
                },
                "result": {
                    "Ok": {
                        "content": [{"type": "text", "text": "------"}],
                        "isError": False,
                    }
                },
            }
        )

        self.assertIsNotNone(normalized)
        invocation, _result, is_error = normalized
        self.assertEqual(invocation["arguments"], {"thoughts": "check"})
        self.assertFalse(is_error)


if __name__ == "__main__":
    unittest.main()
