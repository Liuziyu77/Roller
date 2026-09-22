from __future__ import annotations

import base64
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from roller.sft import (
    SFTConversionError,
    actf_to_sft,
    validate_sft_record,
    write_sft,
)


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
    def test_deep_think_is_an_ordinary_tool_not_reasoning(self) -> None:
        trajectory = _external_trajectory()
        record = actf_to_sft(trajectory)
        assistants = [m for m in record["messages"] if m["role"] == "assistant"]
        self.assertEqual(len(assistants), len(trajectory["steps"]))
        for message, step in zip(assistants, trajectory["steps"]):
            self.assertEqual(
                message["reasoning_content"],
                step["assistant_content"]["reasoning_content"],
            )
        self.assertEqual(record["messages"][0]["content"], trajectory["system_prompt"])
        self.assertIn("deep_think", [tool["function"]["name"] for tool in record["tools"]])
        self.assertEqual(assistants[0]["tool_calls"][0]["function"]["name"], "deep_think")
        self.assertEqual(
            json.loads(assistants[0]["tool_calls"][0]["function"]["arguments"]),
            {"thoughts": "First inspect."},
        )
        self.assertEqual(len([m for m in record["messages"] if m["role"] == "tool"]), 3)

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
            {"type": "audio", "data": "..."}
        ]
        with self.assertRaisesRegex(SFTConversionError, "non-text"):
            actf_to_sft(trajectory)

    def test_text_only_record_keeps_the_exact_current_shape(self) -> None:
        record = actf_to_sft(_trajectory())

        self.assertEqual(set(record), {"messages", "tools"})
        self.assertNotIn("images", record)
        self.assertNotIn("<image>", json.dumps(record))


class MultimodalSFTTests(unittest.TestCase):
    png = base64.b64encode(b"png-bytes").decode()
    jpeg = base64.b64encode(b"jpeg-bytes").decode()
    webp = base64.b64encode(b"webp-bytes").decode()
    gif = base64.b64encode(b"gif-bytes").decode()

    def _trajectory(self, *blocks: dict) -> dict:
        trajectory = _trajectory()
        trajectory["steps"][0]["observation"][0]["content"] = list(blocks)
        return trajectory

    def test_anthropic_source_image_becomes_placeholder_and_uri(self) -> None:
        record = actf_to_sft(
            self._trajectory(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": self.png,
                    },
                }
            )
        )

        self.assertEqual(set(record), {"messages", "tools", "images"})
        self.assertEqual(record["messages"][3]["role"], "tool")
        self.assertEqual(record["messages"][3]["content"], "<image>")
        self.assertEqual(record["images"], [f"data:image/png;base64,{self.png}"])

    def test_flat_native_image_block_and_inline_data_uri(self) -> None:
        flat = actf_to_sft(
            self._trajectory(
                {"type": "image", "data": self.png, "mimeType": "image/png"}
            )
        )
        inline = actf_to_sft(
            self._trajectory(
                {
                    "type": "image",
                    "data": f"data:image/jpeg;base64,{self.jpeg}",
                }
            )
        )

        self.assertEqual(
            flat["images"], [f"data:image/png;base64,{self.png}"]
        )
        self.assertEqual(
            inline["images"], [f"data:image/jpeg;base64,{self.jpeg}"]
        )
        self.assertEqual(flat["messages"][3]["content"], "<image>")
        self.assertEqual(inline["messages"][3]["content"], "<image>")

    def test_keeps_text_blocks_around_the_placeholder_in_order(self) -> None:
        record = actf_to_sft(
            self._trajectory(
                {"type": "text", "text": "Image size: 2x1"},
                {"type": "image", "mimeType": "image/png", "data": self.png},
                {"type": "text", "text": "Image read successfully"},
            )
        )

        self.assertEqual(
            record["messages"][3]["content"],
            "Image size: 2x1\n<image>\nImage read successfully",
        )
        self.assertEqual(len(record["images"]), 1)

    def test_user_and_tool_images_share_message_order(self) -> None:
        trajectory = self._trajectory(
            {"type": "image", "mimeType": "image/jpeg", "data": self.jpeg}
        )
        trajectory["steps"][0]["user_content"] = [
            {"type": "text", "text": "Describe this input"},
            {"type": "image", "mimeType": "image/png", "data": self.png},
        ]

        record = actf_to_sft(trajectory)

        self.assertEqual(record["messages"][1]["content"], "Describe this input\n<image>")
        self.assertEqual(record["messages"][3]["content"], "<image>")
        self.assertEqual(
            record["images"],
            [
                f"data:image/png;base64,{self.png}",
                f"data:image/jpeg;base64,{self.jpeg}",
            ],
        )

    def test_accepts_every_supported_media_type(self) -> None:
        for payload, media_type in (
            (self.png, "image/png"),
            (self.jpeg, "image/jpeg"),
            (self.webp, "image/webp"),
            (self.gif, "image/gif"),
        ):
            with self.subTest(media_type=media_type):
                record = actf_to_sft(
                    self._trajectory(
                        {"type": "image", "mimeType": media_type, "data": payload}
                    )
                )
                self.assertEqual(record["images"], [f"data:{media_type};base64,{payload}"])

    def test_orders_images_across_steps_and_within_one_result(self) -> None:
        trajectory = self._trajectory(
            {"type": "image", "mimeType": "image/png", "data": self.png}
        )
        second = deepcopy(trajectory["steps"][0])
        second["user_content"] = ""
        second["assistant_content"]["tool_calls"][0]["id"] = "call-2"
        second["observation"][0]["toolCallId"] = "call-2"
        second["observation"][0]["content"] = [
            {"type": "text", "text": "two frames"},
            {"type": "image", "mimeType": "image/jpeg", "data": self.jpeg},
            {"type": "image", "mimeType": "image/jpeg", "data": self.jpeg},
        ]
        trajectory["steps"] = [trajectory["steps"][0], second, trajectory["steps"][1]]

        record = actf_to_sft(trajectory)

        self.assertEqual(
            record["images"],
            [
                f"data:image/png;base64,{self.png}",
                f"data:image/jpeg;base64,{self.jpeg}",
                f"data:image/jpeg;base64,{self.jpeg}",
            ],
        )
        tool_contents = [
            message["content"]
            for message in record["messages"]
            if message["role"] == "tool"
        ]
        self.assertEqual(tool_contents, ["<image>", "two frames\n<image>\n<image>"])

    def test_does_not_deduplicate_identical_images(self) -> None:
        record = actf_to_sft(
            self._trajectory(
                {"type": "image", "mimeType": "image/png", "data": self.png},
                {"type": "image", "mimeType": "image/png", "data": self.png},
            )
        )

        self.assertEqual(
            record["images"],
            [f"data:image/png;base64,{self.png}"] * 2,
        )

    def test_rejects_invalid_base64_payload(self) -> None:
        for payload in ("", "not*base64", "aGV sbG8=", "AA="):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(SFTConversionError, "base64"):
                    actf_to_sft(
                        self._trajectory(
                            {
                                "type": "image",
                                "mimeType": "image/png",
                                "data": payload,
                            }
                        )
                    )

    def test_normalizes_base64_before_export_and_validation(self) -> None:
        for payload, canonical in (
            ("AAAA====", "AAAA"), ("YWJj===", "YWJj"), ("AB==", "AA=="),
        ):
            # Strict decoders differ across Python releases on excess padding.
            # Decide the expected outcome independently of Roller's converter.
            try:
                base64.b64decode(payload, validate=True)
            except ValueError:
                rejected_by_decoder = True
            else:
                rejected_by_decoder = False
            for block in (
                {"type": "image", "mimeType": "image/png", "data": payload},
                {"type": "image", "data": f"data:image/png;base64,{payload}"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": payload},
                },
            ):
                with self.subTest(payload=payload, block=block):
                    original = deepcopy(block)
                    if rejected_by_decoder:
                        with self.assertRaisesRegex(SFTConversionError, "base64"):
                            actf_to_sft(self._trajectory(block))
                    else:
                        record = actf_to_sft(self._trajectory(block))
                        self.assertEqual(
                            record["images"], [f"data:image/png;base64,{canonical}"]
                        )
                        validate_sft_record(record)
                    self.assertEqual(block, original)

    def test_rejects_missing_or_unsupported_media_type(self) -> None:
        with self.assertRaisesRegex(SFTConversionError, "mimeType"):
            actf_to_sft(self._trajectory({"type": "image", "data": self.png}))

        with self.assertRaisesRegex(SFTConversionError, "media_type"):
            actf_to_sft(
                self._trajectory(
                    {"type": "image", "source": {"type": "base64", "data": self.png}}
                )
            )

        for media_type in ("image/bmp", "image/svg+xml", "video/mp4"):
            with self.subTest(media_type=media_type):
                with self.assertRaisesRegex(SFTConversionError, "image/"):
                    actf_to_sft(
                        self._trajectory(
                            {
                                "type": "image",
                                "mimeType": media_type,
                                "data": self.png,
                            }
                        )
                    )

    def test_rejects_image_block_without_embedded_bytes(self) -> None:
        with self.assertRaisesRegex(SFTConversionError, "base64 image bytes"):
            actf_to_sft(
                self._trajectory({"type": "image", "path": "/tmp/frame.png"})
            )

        with self.assertRaisesRegex(SFTConversionError, "source.type"):
            actf_to_sft(
                self._trajectory(
                    {
                        "type": "image",
                        "source": {"type": "url", "url": "https://example.com/a.png"},
                    }
                )
            )

    def test_rejects_literal_placeholder_in_user_text_or_reasoning(self) -> None:
        user_conflict = self._trajectory(
            {"type": "image", "mimeType": "image/png", "data": self.png}
        )
        user_conflict["steps"][0]["user_content"] = "describe <image> please"
        with self.assertRaisesRegex(SFTConversionError, "literal <image>"):
            actf_to_sft(user_conflict)

        reasoning_conflict = self._trajectory(
            {"type": "image", "mimeType": "image/png", "data": self.png}
        )
        reasoning_conflict["steps"][0]["assistant_content"]["reasoning_content"] = (
            "The <image> shows a chart."
        )
        with self.assertRaisesRegex(SFTConversionError, "literal <image>"):
            actf_to_sft(reasoning_conflict)

    def test_allows_literal_placeholder_in_text_only_records(self) -> None:
        trajectory = _trajectory()
        trajectory["steps"][0]["user_content"] = "escape <image> literally"

        record = actf_to_sft(trajectory)
        self.assertEqual(set(record), {"messages", "tools"})
        self.assertIn("<image>", record["messages"][1]["content"])

    def test_rejects_literal_placeholder_in_tool_arguments(self) -> None:
        for arguments in (
            {"path": "<image>.png"},
            {"nested": {"values": ["<image>"]}},
            '{"path":"<image>.png"}',
        ):
            with self.subTest(arguments=arguments):
                trajectory = self._trajectory(
                    {"type": "image", "mimeType": "image/png", "data": self.png}
                )
                call = trajectory["steps"][0]["assistant_content"]["tool_calls"][0]
                call["arguments"] = arguments
                with self.assertRaisesRegex(SFTConversionError, "literal <image>"):
                    actf_to_sft(trajectory)
                trajectory["steps"][0]["observation"][0]["content"] = "text only"
                record = actf_to_sft(trajectory)
                self.assertNotIn("images", record)
                saved_call = record["messages"][2]["tool_calls"][0]
                self.assertIn("<image>", saved_call["function"]["arguments"])

    def test_write_sft_persists_the_multimodal_record(self) -> None:
        trajectory = self._trajectory(
            {"type": "image", "mimeType": "image/png", "data": self.png}
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trace_SFT.json"
            expected = write_sft(path, trajectory)
            self.assertEqual(json.loads(path.read_text()), expected)
            self.assertEqual(set(expected), {"messages", "tools", "images"})


class ValidateSFTRecordTests(unittest.TestCase):
    png = base64.b64encode(b"png-bytes").decode()

    def _record(self, with_images: bool = True) -> dict:
        record = {
            "messages": [
                {"role": "system", "content": ""},
                {"role": "user", "content": "read it"},
                {
                    "role": "assistant",
                    "reasoning_content": "",
                    "content": "",
                    "tool_calls": [],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": "<image>"},
            ],
            "tools": [],
        }
        if with_images:
            record["images"] = [f"data:image/png;base64,{self.png}"]
        return record

    def test_accepts_text_only_and_aligned_multimodal_records(self) -> None:
        validate_sft_record(self._record(with_images=False))
        validate_sft_record(self._record())

    def test_rejects_unknown_top_level_keys_and_empty_images(self) -> None:
        record = self._record()
        record["extra"] = 1
        with self.assertRaisesRegex(ValueError, "may only contain"):
            validate_sft_record(record)

        record = self._record()
        record["images"] = []
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            validate_sft_record(record)

        record = self._record()
        record["images"] = "data:image/png;base64,AA=="
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            validate_sft_record(record)

    def test_rejects_malformed_image_entries(self) -> None:
        for uri in (
            "AA==", "data:image/bmp;base64,AA==", "data:image/png;base64,A*",
            "data:image/png;base64,A", "data:image/png;base64,AA=",
            "data:image/png;base64,AAAA====", "data:image/png;base64,AB==",
        ):
            record = self._record()
            record["images"] = [uri]
            with self.subTest(uri=uri), self.assertRaisesRegex(ValueError, "data URI"):
                validate_sft_record(record)

    def test_rejects_placeholder_in_saved_tool_arguments(self) -> None:
        record = self._record()
        record["messages"][2]["tool_calls"] = [{
            "id": "call-1", "type": "function",
            "function": {"name": "read", "arguments": '{"path":"<image>.png"}'},
        }]
        with self.assertRaisesRegex(ValueError, "placeholder count"):
            validate_sft_record(record)

    def test_rejects_placeholder_count_mismatch(self) -> None:
        record = self._record()
        record["messages"][3]["content"] = "text only now"
        with self.assertRaisesRegex(ValueError, "placeholder count"):
            validate_sft_record(record)

        record = self._record()
        record["images"] = record["images"] * 2
        with self.assertRaisesRegex(ValueError, "placeholder count"):
            validate_sft_record(record)


if __name__ == "__main__":
    unittest.main()
