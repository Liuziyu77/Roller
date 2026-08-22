from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from roller.external_thinking import (
    CODEX_DEEP_THINK_TOOL_HANDLE,
    CODEX_EXTERNAL_ALWAYS_ACT_INSTRUCTIONS,
    CODEX_EXTERNAL_ALWAYS_THINK_INSTRUCTIONS,
    CODEX_EXTERNAL_THINKING_INSTRUCTIONS,
    DEEP_THINK_TOOL,
    DEEP_THINK_TOOL_NAME,
    EXTERNAL_ALWAYS_ACT_INSTRUCTIONS,
    EXTERNAL_ALWAYS_THINK_INSTRUCTIONS,
    EXTERNAL_THINKING_INSTRUCTIONS,
    CodexExternalThinkingController,
    ExternalThinkingController,
    external_thinking_mode_from_metadata,
    normalize_external_thinking_mode,
    validate_external_thinking_provider,
    validate_external_thinking_trace,
    validate_external_thinking_trajectory,
)


def _body(input_items=None):
    return {
        "model": "gpt-5.6-sol",
        "input": input_items
        or [
            {"role": "developer", "content": "system"},
            {"role": "user", "content": "solve"},
        ],
        "tools": [
            dict(DEEP_THINK_TOOL),
            {
                "type": "function",
                "name": "exec",
                "description": "execute",
                "parameters": {"type": "object", "properties": {}},
                "strict": False,
            },
        ],
        "reasoning": {"effort": "high", "summary": "auto"},
        "store": True,
    }


def _call(name: str, call_id: str) -> dict:
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": "{}",
    }


def _output(call_id: str) -> dict:
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": "------",
    }


def _codex_body(input_items=None) -> dict:
    return {
        "model": "gpt-5.6-sol",
        "input": input_items
        or [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "functions",
                        "description": "",
                        "tools": [
                            {"type": "custom", "name": "exec", "description": "run JS"},
                            {"type": "function", "name": "wait"},
                        ],
                    },
                    {
                        "type": "namespace",
                        "name": "collaboration",
                        "tools": [{"type": "function", "name": "send_message"}],
                    },
                ],
            },
            {"role": "user", "content": "solve"},
        ],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "reasoning": {"effort": "high", "summary": "detailed"},
        "store": True,
    }


def _codex_think_history(call_id: str = "call_think") -> list[dict]:
    return [
        *_codex_body()["input"],
        {
            "type": "custom_tool_call",
            "call_id": call_id,
            "name": "exec",
            "input": (
                f"const result = await tools.{CODEX_DEEP_THINK_TOOL_HANDLE}"
                '({thoughts: "reason carefully"}); text(result);'
            ),
        },
        {
            "type": "custom_tool_call_output",
            "call_id": call_id,
            "output": [{"type": "input_text", "text": '{"text":"------"}'}],
        },
    ]


def _assistant(*content: dict) -> dict:
    return {"type": "message", "message": {"role": "assistant", "content": list(content)}}


def _think(thoughts: str = "work carefully") -> dict:
    return {
        "type": "toolCall",
        "id": "call_think",
        "name": DEEP_THINK_TOOL_NAME,
        "arguments": {"thoughts": thoughts},
    }


class ExternalThinkingControllerTests(unittest.TestCase):
    def test_normalizes_mode_and_rejects_unknown_value(self) -> None:
        self.assertEqual(normalize_external_thinking_mode("external-always"), "external_always")
        with self.assertRaisesRegex(ValueError, "external thinking mode"):
            normalize_external_thinking_mode("sometimes")

    def test_extracts_mode_from_runtime_or_provenance_metadata(self) -> None:
        self.assertEqual(
            external_thinking_mode_from_metadata(
                {"external_thinking": {"mode": "external_always"}}
            ),
            "external_always",
        )
        self.assertEqual(
            external_thinking_mode_from_metadata(
                {"provenance": {"external_thinking": "external"}}
            ),
            "external",
        )

    def test_external_forces_first_think_then_makes_it_optional(self) -> None:
        controller = ExternalThinkingController("external")
        first, phase = controller.rewrite_request(_body())

        self.assertEqual(phase, "think")
        self.assertEqual(first["reasoning"], {"effort": "none", "summary": "auto"})
        self.assertEqual(first["instructions"], EXTERNAL_THINKING_INSTRUCTIONS)
        self.assertFalse(first["parallel_tool_calls"])
        self.assertFalse(first["store"])
        deep_tool = next(tool for tool in first["tools"] if tool["name"] == "deep_think")
        self.assertTrue(deep_tool["strict"])
        self.assertFalse(deep_tool["parameters"]["additionalProperties"])
        self.assertEqual(
            first["tool_choice"],
            {"type": "function", "name": DEEP_THINK_TOOL_NAME},
        )

        history = [
            {"role": "user", "content": "solve"},
            _call(DEEP_THINK_TOOL_NAME, "call_think"),
            _output("call_think"),
        ]
        second, phase = controller.rewrite_request(_body(history))
        self.assertEqual(phase, "optional")
        self.assertTrue(second["parallel_tool_calls"])
        self.assertNotIn("tool_choice", second)

    def test_external_always_derives_retry_safe_think_act_phases_from_history(self) -> None:
        controller = ExternalThinkingController("external_always")

        first, phase = controller.rewrite_request(_body())
        self.assertEqual(phase, "think")
        self.assertEqual(first["instructions"], EXTERNAL_ALWAYS_THINK_INSTRUCTIONS)
        self.assertEqual(len(first["tools"]), 2)

        thought_history = [
            {"role": "user", "content": "solve"},
            _call(DEEP_THINK_TOOL_NAME, "call_think"),
            _output("call_think"),
        ]
        act, phase = controller.rewrite_request(_body(thought_history))
        self.assertEqual(phase, "act")
        self.assertEqual(act["instructions"], EXTERNAL_ALWAYS_ACT_INSTRUCTIONS)
        self.assertEqual([tool["name"] for tool in act["tools"]], ["exec"])
        self.assertNotIn("tool_choice", act)

        business_history = [
            *thought_history,
            _call("exec", "call_exec"),
            _output("call_exec"),
        ]
        next_think, phase = controller.rewrite_request(_body(business_history))
        self.assertEqual(phase, "think")
        self.assertEqual(len(next_think["tools"]), 2)

        # Rewriting the same retry request derives the same phase; there is no
        # mutable toggle that can drift on HTTP retries.
        retry, retry_phase = controller.rewrite_request(_body(thought_history))
        self.assertEqual(retry_phase, "act")
        self.assertEqual(retry["tools"], act["tools"])

    def test_requires_plugin_tool(self) -> None:
        body = _body()
        body["tools"] = []
        with self.assertRaisesRegex(ValueError, "did not expose"):
            ExternalThinkingController("external").rewrite_request(body)

    def test_provider_validation_requires_responses_and_absolute_base_url(self) -> None:
        valid = {
            "providers": {
                "proxy": {
                    "api": "openai-responses",
                    "baseUrl": "https://example.test/v1",
                }
            }
        }
        validate_external_thinking_provider(valid, "proxy/model", "external")
        with self.assertRaisesRegex(ValueError, "openai-responses"):
            validate_external_thinking_provider(
                {
                    "providers": {
                        "proxy": {
                            "api": "openai-completions",
                            "baseUrl": "https://example.test/v1",
                        }
                    }
                },
                "proxy/model",
                "external",
            )
        with self.assertRaisesRegex(ValueError, "absolute baseUrl"):
            validate_external_thinking_provider(
                {"providers": {"proxy": {"api": "openai-responses"}}},
                "proxy/model",
                "external_always",
            )


class CodexExternalThinkingControllerTests(unittest.TestCase):
    def test_external_forces_successful_first_mcp_think_then_becomes_optional(self) -> None:
        controller = CodexExternalThinkingController("external")

        first, phase = controller.rewrite_request(_codex_body())

        self.assertEqual(phase, "think")
        self.assertEqual(first["reasoning"]["effort"], "none")
        self.assertEqual(first["tool_choice"], "required")
        self.assertFalse(first["parallel_tool_calls"])
        self.assertFalse(first["store"])
        self.assertEqual(first["instructions"], CODEX_EXTERNAL_THINKING_INSTRUCTIONS)
        additional = first["input"][0]["tools"]
        self.assertEqual([namespace["name"] for namespace in additional], ["functions"])
        self.assertEqual(
            [tool["name"] for tool in additional[0]["tools"]],
            ["exec"],
        )

        second, phase = controller.rewrite_request(
            _codex_body(_codex_think_history())
        )
        self.assertEqual(phase, "optional")
        self.assertEqual(second["tool_choice"], "auto")
        self.assertEqual(len(second["input"][0]["tools"]), 2)

    def test_failed_exec_source_does_not_count_as_a_completed_think(self) -> None:
        history = _codex_think_history()
        history[-1]["output"] = "Script failed before MCP returned"

        rewritten, phase = CodexExternalThinkingController("external").rewrite_request(
            _codex_body(history)
        )

        self.assertEqual(phase, "think")
        self.assertEqual(rewritten["tool_choice"], "required")

    def test_external_always_derives_think_act_pairs_from_history(self) -> None:
        controller = CodexExternalThinkingController("external_always")

        first, phase = controller.rewrite_request(_codex_body())
        self.assertEqual(phase, "think")
        self.assertEqual(first["instructions"], CODEX_EXTERNAL_ALWAYS_THINK_INSTRUCTIONS)

        act, phase = controller.rewrite_request(_codex_body(_codex_think_history()))
        self.assertEqual(phase, "act")
        self.assertEqual(act["instructions"], CODEX_EXTERNAL_ALWAYS_ACT_INSTRUCTIONS)
        self.assertEqual(act["tool_choice"], "auto")
        self.assertEqual(len(act["input"][0]["tools"]), 2)

        business_history = [
            *_codex_think_history(),
            {
                "type": "function_call",
                "call_id": "call_business",
                "name": "shell",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call_business",
                "output": "ok",
            },
        ]
        next_think, phase = controller.rewrite_request(
            _codex_body(business_history)
        )
        self.assertEqual(phase, "think")
        self.assertEqual(next_think["tool_choice"], "required")

    def test_requires_codex_programmatic_exec_tool(self) -> None:
        body = _codex_body()
        body["input"][0]["tools"] = []
        with self.assertRaisesRegex(ValueError, "programmatic custom exec"):
            CodexExternalThinkingController("external").rewrite_request(body)


class ExternalThinkingTraceTests(unittest.TestCase):
    @staticmethod
    def _write(events: list[dict]) -> Path:
        temporary = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        with temporary:
            for event in events:
                temporary.write(json.dumps(event) + "\n")
        return Path(temporary.name)

    def test_validates_external_first_call_and_external_always_pairs(self) -> None:
        external_path = self._write(
            [
                _assistant(_think()),
                _assistant({"type": "text", "text": "answer"}),
            ]
        )
        always_path = self._write(
            [
                _assistant(_think("plan action")),
                _assistant(
                    {
                        "type": "toolCall",
                        "id": "call_exec",
                        "name": "exec",
                        "arguments": {"command": "true"},
                    }
                ),
                _assistant(_think("check result")),
                _assistant({"type": "text", "text": "done"}),
            ]
        )
        try:
            external = validate_external_thinking_trace(external_path, "external")
            always = validate_external_thinking_trace(always_path, "external_always")
        finally:
            external_path.unlink(missing_ok=True)
            always_path.unlink(missing_ok=True)

        self.assertEqual(external.deep_think_calls, 1)
        self.assertEqual(always.deep_think_calls, 2)
        self.assertEqual(always.thought_action_pairs, 2)

    def test_rejects_unpaired_or_empty_thought(self) -> None:
        unpaired = self._write([_assistant(_think())])
        empty = self._write(
            [
                _assistant(_think("")),
                _assistant({"type": "text", "text": "answer"}),
            ]
        )
        try:
            with self.assertRaisesRegex(ValueError, "unpaired"):
                validate_external_thinking_trace(unpaired, "external_always")
            with self.assertRaisesRegex(ValueError, "non-empty"):
                validate_external_thinking_trace(empty, "external_always")
        finally:
            unpaired.unlink(missing_ok=True)
            empty.unlink(missing_ok=True)

    def test_validates_canonical_codex_actf_and_isolation(self) -> None:
        trajectory = {
            "steps": [
                {
                    "assistant_content": {
                        "content": "",
                        "reasoning_content": "",
                        "tool_calls": [_think("plan")],
                    },
                    "observation": [
                        {
                            "toolCallId": "call_think",
                            "isError": False,
                            "content": [{"type": "text", "text": "------"}],
                        }
                    ],
                },
                {
                    "assistant_content": {
                        "content": "done",
                        "reasoning_content": "",
                        "tool_calls": [],
                    }
                },
            ]
        }

        stats = validate_external_thinking_trajectory(
            trajectory,
            "external_always",
            require_isolated_deep_think=True,
        )

        self.assertEqual(stats.deep_think_calls, 1)
        self.assertEqual(stats.thought_action_pairs, 1)
        trajectory["steps"][0]["assistant_content"]["content"] = "leak"
        with self.assertRaisesRegex(ValueError, "user-facing text"):
            validate_external_thinking_trajectory(
                trajectory,
                "external_always",
                require_isolated_deep_think=True,
            )

    def test_rejects_failed_codex_deep_think_result(self) -> None:
        trajectory = {
            "steps": [
                {
                    "assistant_content": {
                        "content": "",
                        "tool_calls": [_think("plan")],
                    },
                    "observation": [
                        {
                            "toolCallId": "call_think",
                            "isError": True,
                            "content": [{"type": "text", "text": "failed"}],
                        }
                    ],
                },
                {
                    "assistant_content": {
                        "content": "done",
                        "tool_calls": [],
                    },
                    "observation": [],
                },
            ]
        }

        with self.assertRaisesRegex(ValueError, "returned an error"):
            validate_external_thinking_trajectory(
                trajectory,
                "external_always",
                require_isolated_deep_think=True,
            )


if __name__ == "__main__":
    unittest.main()
