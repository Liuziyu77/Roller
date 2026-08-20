from __future__ import annotations

import unittest

from roller.actf import openclaw_events_to_actf


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


if __name__ == "__main__":
    unittest.main()
