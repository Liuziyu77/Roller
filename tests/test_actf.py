from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path

from roller.actf import openclaw_events_to_actf, convert_openclaw_trace
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
    def test_non_object_native_message_does_not_break_default_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [
                {"type": "message", "message": {"role": "user", "content": "task"}},
                {"type": "message", "message": {"role": "assistant", "content": "done"}},
                {"type": "custom", "message": None},
                {"type": "custom", "message": "unexpected"},
            ]
            source = root / "trace.jsonl"
            source.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            trajectory = convert_openclaw_trace(source, root / "out.json", user_messages=["task"])
            self.assertEqual(len(trajectory["steps"]), 1)
            self.assertEqual(trajectory["steps"][0]["assistant_content"]["content"], "done")

    def test_null_announcement_session_key_does_not_break_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "trace.jsonl"
            source.write_text("\n".join(json.dumps(row) for row in _events()) + "\n")
            cache = root / "cache.jsonl"
            cache.write_text(json.dumps({"runId": "announce:v1:child:run", "sessionKey": None,
                                         "stage": "stream:context", "messages": []}) + "\n")
            trajectory = convert_openclaw_trace(source, root / "out.json", cache_trace=cache)
            self.assertEqual(len(trajectory["steps"]), 2)
            self.assertIn("sft_unavailable_reason", trajectory)

    def test_announcement_input_uses_native_provider_response_binding(self):
        notice = "A background task completed.\nsource: subagent\nCHILD_DONE"
        signature = json.dumps({"id": "provider-final"})
        assistant = {"role": "assistant", "content": [{"type": "text", "text": "Done", "textSignature": signature}], "stopReason": "stop"}
        events = [{"type": "message", "message": {"role": "user", "content": "task"}}, {"type": "message", "message": assistant}]
        base = {"runId": "announce:v1:child:run", "sessionKey": "agent:main:root"}
        for complete in (True, False):
            with self.subTest(complete=complete), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cache_rows = [{**base, "stage": "stream:context", "messages": [{"role": "user", "content": notice}]}]
                if complete:
                    cache_rows.append({**base, "stage": "session:after", "messages": [assistant]})
                (root / "trace.jsonl").write_text("\n".join(json.dumps(e) for e in events))
                (root / "cache.jsonl").write_text("\n".join(json.dumps(e) for e in cache_rows))
                trajectory = convert_openclaw_trace(root / "trace.jsonl", root / "out.json", user_messages=["task"], cache_trace=root / "cache.jsonl")
                if complete:
                    self.assertEqual(trajectory["steps"][0]["user_content"], notice)
                    self.assertNotIn("sft_unavailable_reason", trajectory)
                else:
                    self.assertIn("sft_unavailable_reason", trajectory)


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

    def test_preserves_image_tool_result_and_exports_multimodal_sft(self) -> None:
        events = _events()
        data_url = "data:image/png;base64,AA=="
        events[3]["message"]["content"] = [
            {"type": "text", "text": "image"},
            {"type": "image", "data": data_url},
        ]
        trajectory = openclaw_events_to_actf(
            events,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        )

        self.assertEqual(
            trajectory["steps"][0]["observation"][0]["content"][1],
            {"type": "image", "data": data_url},
        )
        sft = actf_to_sft(trajectory)
        tool_result = next(m for m in sft["messages"] if m["role"] == "tool")
        self.assertEqual(tool_result["content"], "image\n<image>")
        self.assertEqual(sft["images"], [data_url])

    def test_tool_call_ids_are_namespaced_across_sessions(self) -> None:
        first = _events()[:4]
        second = [
            {"type": "session", "id": "roller-2", "timestamp": 2000},
            {
                "type": "message",
                "message": {"role": "user", "content": "second", "timestamp": 2100},
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "read",
                            "arguments": {"path": "inputs/b.md"},
                        }
                    ],
                    "stopReason": "toolUse",
                    "timestamp": 2200,
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "toolName": "read",
                    "content": [{"type": "text", "text": "world"}],
                    "timestamp": 2300,
                },
            },
        ]

        trajectory = openclaw_events_to_actf(
            [*first, *second],
            user_messages=("first", "second"),
            tools=[{"type": "function", "function": {"name": "read"}}],
        )

        call_ids = [
            step["assistant_content"]["tool_calls"][0]["id"]
            for step in trajectory["steps"]
        ]
        observation_ids = [
            step["observation"][0]["toolCallId"] for step in trajectory["steps"]
        ]
        self.assertEqual(call_ids, ["session-1:call-1", "session-2:call-1"])
        self.assertEqual(observation_ids, call_ids)
        self.assertEqual(
            [
                message["tool_call_id"]
                for message in actf_to_sft(trajectory)["messages"]
                if message["role"] == "tool"
            ],
            call_ids,
        )

    def test_empty_assistant_event_after_tool_result_is_not_exported(self) -> None:
        events = _events()
        events.insert(
            4,
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "timestamp": 1300,
                },
            },
        )

        trajectory = openclaw_events_to_actf(
            events,
            user_messages=("clean first", "clean second"),
            tools=[{"type": "function", "function": {"name": "read"}}],
        )

        self.assertEqual(len(trajectory["steps"]), 2)
        self.assertEqual(
            [message["role"] for message in actf_to_sft(trajectory)["messages"]],
            ["system", "user", "assistant", "tool", "user", "assistant"],
        )

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

    def test_empty_failed_attempt_and_automatic_retry_are_not_training_steps(self) -> None:
        events = [
            {"type": "session", "timestamp": "1970-01-01T00:00:00Z"},
            {
                "type": "message",
                "message": {"role": "user", "content": "say ready", "timestamp": 1000},
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": ""}],
                    "stopReason": "aborted",
                    "errorMessage": "LLM idle timeout (120s)",
                    "timestamp": 2000,
                },
            },
            {
                "type": "message",
                "message": {"role": "user", "content": "say ready", "timestamp": 2100},
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ready"}],
                    "stopReason": "stop",
                    "timestamp": 3000,
                },
            },
        ]

        trajectory = openclaw_events_to_actf(events, user_messages=("say ready",))
        self.assertEqual(len(trajectory["steps"]), 1)
        self.assertEqual(trajectory["steps"][0]["user_content"], "say ready")
        self.assertEqual(trajectory["steps"][0]["assistant_content"]["content"], "ready")
        self.assertEqual(
            [message["role"] for message in actf_to_sft(trajectory)["messages"]],
            ["system", "user", "assistant"],
        )


if __name__ == "__main__":
    unittest.main()
