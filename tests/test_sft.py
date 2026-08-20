from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from roller.sft import SFTConversionError, actf_to_sft, write_sft


def _trajectory() -> dict:
    return {
        "schema_version": "ACTF_v1.0",
        "system_prompt": "Use tools carefully.",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read a file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                },
            }
        ],
        "steps": [
            {
                "step_id": 1,
                "user_content": "Read input.txt",
                "assistant_content": {
                    "reasoning_content": "I need the file.",
                    "content": "",
                    "tool_calls": [
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "read",
                            "arguments": {"path": "input.txt"},
                        }
                    ],
                },
                "observation": [
                    {
                        "role": "toolResult",
                        "toolCallId": "call-1",
                        "toolName": "read",
                        "content": [
                            {"type": "text", "text": "line one"},
                            {"type": "text", "text": "line two"},
                        ],
                        "isError": False,
                        "details": {"runtime": "discarded"},
                    }
                ],
                "metric": {"prompt_tokens_len": 10},
                "started_at": "discarded",
                "finished_at": "discarded",
            },
            {
                "step_id": 2,
                "user_content": "",
                "assistant_content": {
                    "reasoning_content": "Now summarize.",
                    "content": "Summary",
                    "tool_calls": [],
                },
                "observation": [],
                "metric": {},
            },
        ],
        "started_at": "discarded",
        "finished_at": "discarded",
    }


class SFTConversionTests(unittest.TestCase):
    def test_projects_minimal_messages_and_tools(self) -> None:
        record = actf_to_sft(_trajectory())

        self.assertEqual(set(record), {"messages", "tools"})
        self.assertEqual(
            [message["role"] for message in record["messages"]],
            ["system", "user", "assistant", "tool", "assistant"],
        )
        first_assistant = record["messages"][2]
        self.assertEqual(
            set(first_assistant),
            {"role", "reasoning_content", "content", "tool_calls"},
        )
        self.assertEqual(first_assistant["reasoning_content"], "I need the file.")
        call = first_assistant["tool_calls"][0]
        self.assertEqual(call["type"], "function")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"path": "input.txt"})
        self.assertEqual(
            record["messages"][3],
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "line one\nline two",
            },
        )
        self.assertEqual(
            record["messages"][4]["tool_calls"],
            [],
        )
        serialized = json.dumps(record)
        for removed in ("schema_version", "metric", "started_at", "isError", "details"):
            self.assertNotIn(removed, serialized)

    def test_writes_exact_record_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trace_SFT.json"
            expected = write_sft(path, _trajectory())
            self.assertEqual(json.loads(path.read_text()), expected)

    def test_allows_empty_tools_only_without_calls(self) -> None:
        trajectory = _trajectory()
        trajectory["tools"] = None
        trajectory["steps"] = [trajectory["steps"][1]]
        trajectory["steps"][0]["user_content"] = "Answer directly"

        record = actf_to_sft(trajectory)
        self.assertEqual(record["tools"], [])

    def test_normalizes_flat_responses_api_tools(self) -> None:
        trajectory = _trajectory()
        trajectory["tools"] = [
            {
                "type": "function",
                "name": "read",
                "description": "Read a file",
                "parameters": {"type": "object"},
                "strict": False,
            }
        ]

        tool = actf_to_sft(trajectory)["tools"][0]
        self.assertEqual(set(tool), {"type", "function"})
        self.assertEqual(tool["function"]["name"], "read")
        self.assertFalse(tool["function"]["strict"])

    def test_rejects_missing_tool_schema_or_result(self) -> None:
        missing_schema = _trajectory()
        missing_schema["tools"] = None
        with self.assertRaisesRegex(SFTConversionError, "missing schema"):
            actf_to_sft(missing_schema)

        missing_result = _trajectory()
        missing_result["steps"][0]["observation"] = []
        with self.assertRaisesRegex(SFTConversionError, "missing results"):
            actf_to_sft(missing_result)

    def test_rejects_non_text_tool_results(self) -> None:
        trajectory = _trajectory()
        trajectory["steps"][0]["observation"][0]["content"] = [
            {"type": "image", "data": "..."}
        ]
        with self.assertRaisesRegex(SFTConversionError, "non-text"):
            actf_to_sft(trajectory)


if __name__ == "__main__":
    unittest.main()
