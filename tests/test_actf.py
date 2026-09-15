from __future__ import annotations

import unittest

from roller.actf import openclaw_events_to_actf
from roller.sft import actf_to_sft


def _events() -> list[dict]:
    return [
        {"type": "session", "timestamp": "1970-01-01T00:00:00Z"},
        {
            "type": "message",
            "message": {"role": "user", "content": "first", "timestamp": 1000},
        },
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "inspect"},
                    {
                        "type": "toolCall",
                        "id": "call-1",
                        "name": "read",
                        "arguments": {"path": "inputs/a.md"},
                    },
                ],
                "usage": {"input": 10, "output": 3},
                "stopReason": "toolUse",
                "timestamp": 1100,
            },
        },
        {
            "type": "message",
            "message": {
                "role": "toolResult",
                "toolCallId": "call-1",
                "toolName": "read",
                "content": [{"type": "text", "text": "hello"}],
                "isError": False,
                "timestamp": 1200,
            },
        },
        {
            "type": "message",
            "message": {"role": "user", "content": "second", "timestamp": 2000},
        },
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
                "usage": {"input": 15, "output": 1},
                "stopReason": "stop",
                "timestamp": 2100,
            },
        },
    ]


class ActfConversionTests(unittest.TestCase):
    def test_groups_assistant_tool_results_and_multiple_user_turns(self) -> None:
        trajectory = openclaw_events_to_actf(
            _events(), user_messages=("clean first", "clean second")
        )

        self.assertEqual(
            set(trajectory),
            {
                "schema_version",
                "system_prompt",
                "tools",
                "steps",
                "started_at",
                "finished_at",
            },
        )
        self.assertEqual(trajectory["schema_version"], "ACTF_v1.0")
        self.assertEqual(trajectory["system_prompt"], "")
        self.assertIsNone(trajectory["tools"])
        self.assertEqual(len(trajectory["steps"]), 2)
        first, second = trajectory["steps"]
        self.assertEqual(
            set(first),
            {
                "step_id",
                "assistant_content",
                "metric",
                "user_content",
                "tools",
                "observation",
                "started_at",
                "finished_at",
            },
        )
        self.assertEqual(first["user_content"], "clean first")
        self.assertEqual(first["assistant_content"]["reasoning_content"], "inspect")
        self.assertEqual(first["tools"], first["assistant_content"]["tool_calls"])
        self.assertEqual(first["observation"][0]["toolCallId"], "call-1")
        self.assertEqual(first["metric"]["prompt_tokens_len"], 10)
        self.assertEqual(first["finished_at"], "1970-01-01 00:00:01.200000")
        self.assertEqual(second["user_content"], "clean second")
        self.assertEqual(second["assistant_content"]["content"], "done")
        self.assertEqual(second["metric"]["stop_reason"], "stop")

    def test_system_prompt_and_tools_stored_once_at_top_level(self) -> None:
        trajectory = openclaw_events_to_actf(
            _events(),
            system_prompts=["shared system prompt"],
            tools=[{"type": "function", "function": {"name": "read"}}],
        )

        self.assertEqual(trajectory["system_prompt"], "shared system prompt")
        self.assertEqual(len(trajectory["tools"]), 1)
        for step in trajectory["steps"]:
            self.assertNotIn("system_prompt", step)

    def test_keep_thinking_false_blanks_reasoning_content(self) -> None:
        trajectory = openclaw_events_to_actf(_events(), keep_thinking=False)

        for step in trajectory["steps"]:
            self.assertEqual(step["assistant_content"]["reasoning_content"], "")

        kept = openclaw_events_to_actf(_events(), keep_thinking=True)
        self.assertEqual(kept["steps"][0]["assistant_content"]["reasoning_content"], "inspect")

    def test_cli_delivery_copy_excluded_from_steps_and_sft(self) -> None:
        reply_usage = {"input": 15, "output": 1}
        events = [
            {"type": "session", "timestamp": "1970-01-01T00:00:00Z"},
            {
                "type": "message",
                "message": {"role": "user", "content": "hi", "timestamp": 1000},
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "api": "openai-completions",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": reply_usage,
                    "stopReason": "stop",
                    "timestamp": 1100,
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "api": "cli",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": dict(reply_usage),
                    "stopReason": "stop",
                    "timestamp": 1150,
                },
            },
        ]

        trajectory = openclaw_events_to_actf(events, user_messages=("hi",))

        self.assertEqual(len(trajectory["steps"]), 1)
        step = trajectory["steps"][0]
        self.assertEqual(step["assistant_content"]["content"], "done")
        self.assertEqual(step["metric"]["prompt_tokens_len"], 15)
        self.assertEqual(step["finished_at"], "1970-01-01 00:00:01.100000")

        sft = actf_to_sft(trajectory)
        self.assertEqual(
            [message["role"] for message in sft["messages"]],
            ["system", "user", "assistant"],
        )

    def test_cli_delivery_copy_after_tool_turn_preserves_grouping(self) -> None:
        events = [
            *_events(),
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "api": "cli",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {"input": 15, "output": 1},
                    "stopReason": "stop",
                    "timestamp": 2200,
                },
            },
        ]

        trajectory = openclaw_events_to_actf(
            events, user_messages=("clean first", "clean second")
        )

        self.assertEqual(len(trajectory["steps"]), 2)
        self.assertEqual(trajectory["steps"][1]["assistant_content"]["content"], "done")
        self.assertEqual(
            trajectory["steps"][1]["finished_at"], "1970-01-01 00:00:02.100000"
        )


if __name__ == "__main__":
    unittest.main()
