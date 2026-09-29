from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from roller.codex_actf import (
    codex_events_to_actf,
    convert_codex_trace,
)
from roller.sft import SFTConversionError, actf_to_sft
from roller.usage import extract_codex_usage


_TOOLS = [
    {"type": "function", "name": "exec_command", "description": "Run a command.",
     "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}},
    {"type": "function", "name": "view_image", "description": "View an image.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"type": "function", "name": "deep_think", "description": "Ordinary fixture tool.",
     "parameters": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}},
    {"type": "function", "name": "unused", "parameters": {"type": "object", "properties": {}}},
    {"type": "web_search"},
]


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
    def test_deep_think_name_has_no_special_schema_or_reasoning_projection(self) -> None:
        events = _events()
        events[4]["payload"]["name"] = "deep_think"
        events[4]["payload"]["arguments"] = '{"note":"ordinary tool argument"}'
        trajectory = codex_events_to_actf(events, tools=_TOOLS)
        record = actf_to_sft(trajectory)
        tool = next(tool for tool in record["tools"] if tool.get("name") == "deep_think")
        self.assertEqual(tool["name"], "deep_think")
        self.assertEqual(tool["parameters"]["properties"], {"note": {"type": "string"}})
        assistant = next(m for m in record["messages"] if m["role"] == "assistant")
        self.assertEqual(assistant["reasoning_content"], "Inspect the file.")
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "deep_think")

    def test_groups_native_events_by_model_request(self) -> None:
        trajectory = codex_events_to_actf(
            _events(), user_messages=["read input.txt"], tools=_TOOLS,
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
        self.assertEqual(trajectory["tools"][0]["name"], "exec_command")

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

    def test_preserves_view_image_output_for_multimodal_sft(self) -> None:
        events = _events()
        events[4]["payload"]["name"] = "view_image"
        events[4]["payload"]["arguments"] = '{"path":"/workspace/image.png"}'
        events[6]["payload"]["output"] = [
            {
                "type": "input_image",
                "image_url": "data:image/png;base64,AA==",
                "detail": "high",
            }
        ]

        trajectory = codex_events_to_actf(events, user_messages=["inspect image.png"], tools=_TOOLS)

        self.assertEqual(
            trajectory["steps"][0]["observation"][0]["content"],
            [
                {
                    "type": "image",
                    "data": "data:image/png;base64,AA==",
                    "detail": "high",
                }
            ],
        )
        sft = actf_to_sft(trajectory)
        self.assertEqual(sft["images"], ["data:image/png;base64,AA=="])
        tool_result = next(
            message for message in sft["messages"] if message["role"] == "tool"
        )
        self.assertEqual(tool_result["content"], "<image>")

    def test_preserves_text_and_multiple_view_images_in_order(self) -> None:
        events = _events()
        events[4]["payload"]["name"] = "view_image"
        events[4]["payload"]["arguments"] = '{"path":"/workspace/images"}'
        events[6]["payload"]["output"] = [
            {"type": "text", "text": "two frames"},
            {"type": "input_image", "image_url": "data:image/png;base64,AA=="},
            {"type": "input_image", "image_url": "data:image/jpeg;base64,AQ=="},
        ]

        record = actf_to_sft(
            codex_events_to_actf(events, user_messages=["inspect images"], tools=_TOOLS)
        )
        tool_result = next(
            message for message in record["messages"] if message["role"] == "tool"
        )
        self.assertEqual(tool_result["content"], "two frames\n<image>\n<image>")
        self.assertEqual(
            record["images"],
            ["data:image/png;base64,AA==", "data:image/jpeg;base64,AQ=="],
        )

    def test_preserves_ordinary_json_array_tool_outputs(self) -> None:
        cases = (
            ("[]", "[]"),
            ("[1, 2]", "[1, 2]"),
            ('["left", "right"]', '["left", "right"]'),
            (
                '{"output": [1, 2], "metadata": {"exit_code": 0}}',
                "[1, 2]",
            ),
        )
        for native_output, expected in cases:
            with self.subTest(native_output=native_output):
                events = _events()
                events[6]["payload"]["output"] = native_output
                trajectory = codex_events_to_actf(
                    events, user_messages=["Read values"], tools=_TOOLS,
                )
                self.assertEqual(
                    trajectory["steps"][0]["observation"][0]["content"],
                    [{"type": "text", "text": expected}],
                )
                record = actf_to_sft(trajectory)
                tool_result = next(
                    message
                    for message in record["messages"]
                    if message["role"] == "tool"
                )
                self.assertEqual(tool_result["content"], expected)

    def test_preserves_ordinary_non_content_block_arrays(self) -> None:
        cases = (
            ([1, 2], "[1, 2]"),
            (["left", "right"], '["left", "right"]'),
            (
                [{"type": "text", "text": "left"}, {"value": "right"}],
                '[{"type": "text", "text": "left"}, {"value": "right"}]',
            ),
        )
        for native_output, expected in cases:
            with self.subTest(native_output=native_output):
                events = _events()
                events[6]["payload"]["output"] = native_output
                record = actf_to_sft(
                    codex_events_to_actf(events, user_messages=["Read values"], tools=_TOOLS)
                )
                tool_result = next(
                    message
                    for message in record["messages"]
                    if message["role"] == "tool"
                )
                self.assertEqual(tool_result["content"], expected)

    def test_rejects_malformed_typed_text_block(self) -> None:
        events = _events()
        events[6]["payload"]["output"] = [
            {"type": "text", "text": 7, "metadata": "not ACTF content"}
        ]

        trajectory = codex_events_to_actf(events, user_messages=["Read values"], tools=_TOOLS)

        self.assertEqual(
            trajectory["steps"][0]["observation"][0]["content"],
            [{"type": "text", "text": 7}],
        )
        with self.assertRaisesRegex(SFTConversionError, "text must be a string"):
            actf_to_sft(trajectory)

    def test_rejects_missing_image_data_without_downgrading_media(self) -> None:
        cases = (
            (
                "native missing image",
                [{"type": "input_image"}],
                [{"type": "image"}],
            ),
            (
                "native valid and missing images",
                [
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64,AA==",
                    },
                    {"type": "input_image"},
                ],
                [
                    {"type": "image", "data": "data:image/png;base64,AA=="},
                    {"type": "image"},
                ],
            ),
            (
                "JSON encoded valid and missing images",
                json.dumps(
                    [
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,AA==",
                        },
                        {"type": "input_image"},
                    ]
                ),
                [
                    {"type": "image", "data": "data:image/png;base64,AA=="},
                    {"type": "image"},
                ],
            ),
            (
                "JSON wrapper with valid and missing images",
                json.dumps(
                    {
                        "output": [
                            {
                                "type": "input_image",
                                "image_url": "data:image/png;base64,AA==",
                            },
                            {"type": "input_image"},
                        ],
                        "metadata": {"exit_code": 0},
                    }
                ),
                [
                    {"type": "image", "data": "data:image/png;base64,AA=="},
                    {"type": "image"},
                ],
            ),
        )
        for label, native_output, expected_content in cases:
            with self.subTest(label=label):
                events = _events()
                events[4]["payload"]["name"] = "view_image"
                events[6]["payload"]["output"] = native_output

                trajectory = codex_events_to_actf(
                    events, user_messages=["Inspect images"], tools=_TOOLS,

                )

                self.assertEqual(
                    trajectory["steps"][0]["observation"][0]["content"],
                    expected_content,
                )
                with self.assertRaisesRegex(
                    SFTConversionError, "base64 image bytes"
                ):
                    actf_to_sft(trajectory)

    def test_preserves_image_with_losslessly_serialized_unknown_sibling(self) -> None:
        image = {
            "type": "input_image",
            "image_url": "data:image/png;base64,AA==",
        }
        cases = (
            (
                [image, {"value": "not a content block"}],
                '{"value": "not a content block"}',
            ),
            (json.dumps([image, {"value": 7}]), '{"value": 7}'),
            ([image, 7], "7"),
        )
        for native_output, expected_text in cases:
            with self.subTest(native_output=native_output):
                events = _events()
                events[4]["payload"]["name"] = "view_image"
                events[6]["payload"]["output"] = native_output

                trajectory = codex_events_to_actf(
                    events, user_messages=["Inspect images"], tools=_TOOLS,

                )

                self.assertEqual(
                    trajectory["steps"][0]["observation"][0]["content"],
                    [
                        {"type": "image", "data": "data:image/png;base64,AA=="},
                        {"type": "text", "text": expected_text},
                    ],
                )
                record = actf_to_sft(trajectory)
                tool_result = next(
                    message
                    for message in record["messages"]
                    if message["role"] == "tool"
                )
                self.assertEqual(tool_result["content"], f"<image>\n{expected_text}")
                self.assertEqual(
                    record["images"], ["data:image/png;base64,AA=="]
                )

    def test_rejects_unsupported_typed_file_and_audio_blocks(self) -> None:
        cases = (
            {
                "type": "input_file",
                "filename": "report.pdf",
                "file_data": "AA==",
            },
            {"type": "input_audio", "input_audio": {"data": "AA=="}},
        )
        for block in cases:
            for native_output in ([block], json.dumps([block])):
                with self.subTest(native_output=native_output):
                    events = _events()
                    events[6]["payload"]["output"] = native_output

                    trajectory = codex_events_to_actf(
                        events, user_messages=["Inspect result"], tools=_TOOLS,

                    )

                    self.assertEqual(
                        trajectory["steps"][0]["observation"][0]["content"],
                        [block],
                    )
                    with self.assertRaisesRegex(SFTConversionError, "non-text"):
                        actf_to_sft(trajectory)

    def test_preserves_ordinary_json_object_tool_output(self) -> None:
        events = _events()
        events[6]["payload"]["output"] = '{"values": [1, 2]}'

        record = actf_to_sft(
            codex_events_to_actf(events, user_messages=["Read values"], tools=_TOOLS)
        )
        tool_result = next(
            message for message in record["messages"] if message["role"] == "tool"
        )

        self.assertEqual(tool_result["content"], '{"values": [1, 2]}')

    def test_distinguishes_json_empty_array_from_empty_content_blocks(self) -> None:
        json_events = _events()
        json_events[6]["payload"]["output"] = "[]"
        block_events = _events()
        block_events[6]["payload"]["output"] = []

        json_trajectory = codex_events_to_actf(
            json_events, user_messages=["Read values"], tools=_TOOLS,
)
        block_trajectory = codex_events_to_actf(
            block_events, user_messages=["Read values"], tools=_TOOLS,
)

        self.assertEqual(
            json_trajectory["steps"][0]["observation"][0]["content"],
            [{"type": "text", "text": "[]"}],
        )
        self.assertEqual(
            block_trajectory["steps"][0]["observation"][0]["content"], []
        )

    def test_expands_json_encoded_typed_image_blocks(self) -> None:
        events = _events()
        events[6]["payload"]["output"] = json.dumps(
            [
                {"type": "text", "text": "frame"},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,AA==",
                },
            ]
        )

        record = actf_to_sft(
            codex_events_to_actf(events, user_messages=["inspect frame"], tools=_TOOLS)
        )
        tool_result = next(
            message for message in record["messages"] if message["role"] == "tool"
        )
        self.assertEqual(tool_result["content"], "frame\n<image>")
        self.assertEqual(record["images"], ["data:image/png;base64,AA=="])

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

        trajectory = codex_events_to_actf(events, user_messages=["read input.txt"], tools=_TOOLS)

        self.assertIn("<environment_context>", trajectory["system_prompt"])
        self.assertEqual(trajectory["steps"][0]["user_content"], "read input.txt")
        self.assertEqual(
            trajectory["steps"][0]["assistant_content"]["reasoning_content"],
            "Visible full reasoning.",
        )


if __name__ == "__main__":
    unittest.main()
