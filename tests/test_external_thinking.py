from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from roller.external_thinking import (
    DEEP_THINK_TOOL,
    DEEP_THINK_TOOL_NAME,
    EXTERNAL_ALWAYS_ACT_INSTRUCTIONS,
    EXTERNAL_ALWAYS_THINK_INSTRUCTIONS,
    EXTERNAL_THINKING_INSTRUCTIONS,
    ExternalThinkingController,
    external_thinking_mode_from_metadata,
    normalize_external_thinking_mode,
    validate_external_thinking_provider,
    validate_external_thinking_trace,
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


if __name__ == "__main__":
    unittest.main()
