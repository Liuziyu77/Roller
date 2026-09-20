from __future__ import annotations

import base64
from copy import deepcopy
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


def _external_trajectory() -> dict:
    deep_tool = {
        "type": "function",
        "function": {
            "name": "deep_think",
            "description": "scratchpad",
            "parameters": {
                "type": "object",
                "properties": {"thoughts": {"type": "string"}},
                "required": ["thoughts"],
            },
        },
    }
    read_tool = _trajectory()["tools"][0]

    def assistant(calls, *, content="", reasoning=""):
        return {
            "reasoning_content": reasoning,
            "content": content,
            "tool_calls": calls,
        }

    def call(call_id, name, arguments):
        return {
            "type": "toolCall",
            "id": call_id,
            "name": name,
            "arguments": arguments,
        }

    def result(call_id, name, content):
        return {
            "role": "toolResult",
            "toolCallId": call_id,
            "toolName": name,
            "content": [{"type": "text", "text": content}],
        }

    return {
        "schema_version": "ACTF_v1.0",
        "system_prompt": "Use tools carefully.\n- deep_think\n- read\n",
        "tools": [deep_tool, read_tool],
        "steps": [
            {
                "user_content": "Read input.txt",
                "assistant_content": assistant(
                    [call("think-1", "deep_think", {"thoughts": "First inspect."})]
                ),
                "observation": [result("think-1", "deep_think", "------")],
            },
            {
                "user_content": "",
                "assistant_content": assistant(
                    [call("read-1", "read", {"path": "input.txt"})]
                ),
                "observation": [result("read-1", "read", "file contents")],
            },
            {
                "user_content": "",
                "assistant_content": assistant(
                    [call("think-2", "deep_think", {"thoughts": "Now answer."})]
                ),
                "observation": [result("think-2", "deep_think", "------")],
            },
            {
                "user_content": "",
                "assistant_content": assistant([], content="Done"),
                "observation": [],
            },
        ],
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

    def test_folds_external_thinking_into_following_assistant_reasoning(self) -> None:
        record = actf_to_sft(
            _external_trajectory(), external_thinking="external_always"
        )

        self.assertEqual(
            [message["role"] for message in record["messages"]],
            ["system", "user", "assistant", "tool", "assistant"],
        )
        first_assistant = record["messages"][2]
        self.assertEqual(first_assistant["reasoning_content"], "First inspect.")
        self.assertEqual(
            [call["function"]["name"] for call in first_assistant["tool_calls"]],
            ["read"],
        )
        self.assertEqual(record["messages"][4]["reasoning_content"], "Now answer.")
        self.assertEqual(record["messages"][4]["content"], "Done")
        self.assertEqual(
            [tool["function"]["name"] for tool in record["tools"]], ["read"]
        )
        self.assertNotIn("deep_think", json.dumps(record))
        self.assertNotIn("------", json.dumps(record))

    def test_keeps_deep_think_as_a_tool_when_external_mode_is_off(self) -> None:
        record = actf_to_sft(_external_trajectory())

        self.assertIn(
            "deep_think", [tool["function"]["name"] for tool in record["tools"]]
        )
        self.assertIn("- deep_think", record["messages"][0]["content"])
        self.assertEqual(
            record["messages"][2]["tool_calls"][0]["function"]["name"],
            "deep_think",
        )

    def test_folds_external_think_batched_with_a_business_tool(self) -> None:
        trajectory = _external_trajectory()
        think_step = trajectory["steps"][0]
        business_step = trajectory["steps"][1]
        think_step["assistant_content"]["tool_calls"].extend(
            business_step["assistant_content"]["tool_calls"]
        )
        think_step["observation"].extend(business_step["observation"])
        trajectory["steps"] = [think_step]

        record = actf_to_sft(trajectory, external_thinking="external")

        assistant = record["messages"][2]
        self.assertEqual(assistant["reasoning_content"], "First inspect.")
        self.assertEqual(
            [call["function"]["name"] for call in assistant["tool_calls"]],
            ["read"],
        )
        self.assertEqual(
            record["messages"][3],
            {"role": "tool", "tool_call_id": "read-1", "content": "file contents"},
        )

    def test_rejects_external_thought_without_following_action(self) -> None:
        trajectory = _external_trajectory()
        trajectory["steps"] = trajectory["steps"][:1]

        with self.assertRaisesRegex(SFTConversionError, "following assistant action"):
            actf_to_sft(trajectory, external_thinking="external")

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
        record = actf_to_sft(missing_schema)
        inferred = record["tools"][0]["function"]
        self.assertEqual(inferred["name"], "read")
        self.assertTrue(inferred["parameters"]["additionalProperties"])
        self.assertIn("inferred", inferred["description"])

        missing_result = _trajectory()
        missing_result["tools"] = None
        missing_result["steps"][0]["observation"] = []
        with self.assertRaisesRegex(SFTConversionError, "missing schema"):
            actf_to_sft(missing_result)

    def test_does_not_infer_schema_from_mismatched_tool_result(self) -> None:
        trajectory = _trajectory()
        trajectory["tools"] = None
        trajectory["steps"][0]["observation"][0]["toolName"] = "write"

        with self.assertRaisesRegex(SFTConversionError, "missing schema"):
            actf_to_sft(trajectory)

    def test_rejects_non_text_tool_results(self) -> None:
        trajectory = _trajectory()
        trajectory["steps"][0]["observation"][0]["content"] = [
            {"type": "audio", "data": "..."}
        ]
        with self.assertRaisesRegex(SFTConversionError, "non-text"):
            actf_to_sft(trajectory)

    def test_preserves_inline_image_bytes_and_content_order(self) -> None:
        trajectory = _trajectory()
        image = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()
        trajectory["steps"][0]["observation"][0]["content"] = [
            {"type": "text", "text": "before"},
            {"type": "image", "data": image, "mimeType": "image/png"},
            "after",
        ]
        original = deepcopy(trajectory)
        record = actf_to_sft(trajectory)
        message = record["messages"][3]
        self.assertEqual(message["tool_call_id"], "call-1")
        self.assertEqual(message["content"], [
            {"type": "text", "text": "before"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
            {"type": "text", "text": "after"},
        ])
        self.assertEqual(trajectory, original)
        self.assertEqual(json.loads(json.dumps(record)), record)

    def test_rejects_malformed_inline_images(self) -> None:
        for block in (
            {"type": "image", "data": "aGk="},
            {"type": "image", "data": "", "mimeType": "image/png"},
            {"type": "image", "data": "***", "mimeType": "image/png"},
            {"type": "image", "data": "aGk=", "mimeType": "text/html"},
            {"type": "image", "data": "aGk=", "mimeType": "image/png;bad"},
        ):
            with self.subTest(block=block):
                trajectory = _trajectory()
                trajectory["steps"][0]["observation"][0]["content"] = [block]
                with self.assertRaisesRegex(SFTConversionError, "image"):
                    actf_to_sft(trajectory)


if __name__ == "__main__":
    unittest.main()
