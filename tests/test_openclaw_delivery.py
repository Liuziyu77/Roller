from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from roller.actf import openclaw_events_to_actf
from roller.sft import actf_to_sft
from roller.usage import extract_openclaw_usage


def _user(text: str = "hi") -> dict:
    return {
        "type": "message",
        "message": {"role": "user", "content": text, "timestamp": 1000},
    }


def _reply(api: str, text: str = "done", **fields) -> dict:
    return {
        "type": "message",
        "message": {
            "role": "assistant",
            "api": api,
            "provider": "provider",
            "model": "model",
            "content": [{"type": "text", "text": text}],
            "stopReason": "stop",
            "timestamp": 1100,
            "usage": {
                "input": 10, "output": 2, "cacheRead": 3, "cacheWrite": 1,
                "totalTokens": 16, "cost": {"total": 0.05},
            },
            **fields,
        },
    }


class OpenClawDeliveryTests(unittest.TestCase):
    def _project(
        self, events: list[dict], *, tools: list[dict] | None = None
    ) -> tuple[dict, dict, dict]:
        original = deepcopy(events)
        actf = openclaw_events_to_actf(events, tools=tools)
        sft = actf_to_sft(actf)
        with tempfile.TemporaryDirectory() as temporary:
            trace = Path(temporary) / "trace.jsonl"
            raw = "".join(json.dumps(event) + "\n" for event in events)
            trace.write_text(raw, encoding="utf-8")
            usage = extract_openclaw_usage(trace)
            self.assertEqual(trace.read_text(encoding="utf-8"), raw)
        self.assertEqual(events, original)
        return actf, sft, usage

    def test_cli_only_reply_keeps_content_and_all_usage_fields(self) -> None:
        actf, sft, usage = self._project([_user(), _reply("cli")])
        self.assertEqual(len(actf["steps"]), 1)
        self.assertEqual(sft["messages"][-1]["content"], "done")
        self.assertEqual(actf["steps"][0]["metric"]["prompt_tokens_len"], 10)
        self.assertEqual(usage, {
            "input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 3,
            "cache_write_tokens": 1, "total_tokens": 16, "cost_usd": 0.05,
            "request_count": 1,
        })

    def test_delivery_copy_keeps_provider_reasoning_and_counts_once(self) -> None:
        original = _reply("openai-completions")
        original["message"]["content"].insert(0, {"type": "thinking", "thinking": "inspect"})
        actf, sft, usage = self._project([_user(), original, _reply("cli")])
        self.assertEqual(len(actf["steps"]), 1)
        self.assertEqual(sft["messages"][-1]["reasoning_content"], "inspect")
        self.assertEqual(usage["total_tokens"], 16)
        self.assertEqual(usage["cache_read_tokens"], 3)
        self.assertEqual(usage["cache_write_tokens"], 1)
        self.assertEqual(usage["cost_usd"], 0.05)
        self.assertEqual(usage["request_count"], 1)

    def test_gap_fill_keeps_new_content_without_recounting_usage(self) -> None:
        actf, sft, usage = self._project([
            _user(), _reply("openai-completions", "draft"),
            _reply("cli", "final answer"),
        ])
        self.assertEqual(len(actf["steps"]), 2)
        self.assertEqual(sft["messages"][-1]["content"], "final answer")
        self.assertEqual(usage["request_count"], 1)
        self.assertEqual(usage["total_tokens"], 16)
        self.assertEqual(actf["steps"][-1]["metric"]["prompt_tokens_len"], 0)
        self.assertEqual(actf["steps"][-1]["metric"]["completion_tokens_len"], 0)

    def test_gap_fill_turn_total_is_not_an_extra_request(self) -> None:
        for final_text, expected_steps in (("second", 2), ("new final", 3)):
            with self.subTest(final_text=final_text):
                actf, sft, usage = self._project([
                    _user(), _reply("openai-completions", "first"),
                    _reply("openai-completions", "second"),
                    _reply("cli", final_text, usage={
                        "input": 20, "output": 4, "cacheRead": 6,
                        "cacheWrite": 2, "totalTokens": 32,
                    }),
                ])
                self.assertEqual(len(actf["steps"]), expected_steps)
                self.assertEqual(sft["messages"][-1]["content"], final_text)
                self.assertEqual(usage["request_count"], 2)
                self.assertEqual(usage["total_tokens"], 32)
                self.assertEqual(usage["cost_usd"], 0.10)
                self.assertEqual(sum(
                    step["metric"]["prompt_tokens_len"] for step in actf["steps"]
                ), 20)

    def test_gap_fill_after_tool_use_preserves_the_only_final_answer(self) -> None:
        provider = _reply(
            "openai-completions", stopReason="toolUse", content=[{
                "type": "toolCall", "id": "read-1", "name": "read",
                "arguments": {"path": "input.txt"},
            }],
        )
        observation = {"type": "message", "message": {
            "role": "toolResult", "toolCallId": "read-1", "toolName": "read",
            "content": [{"type": "text", "text": "file contents"}],
        }}
        tools = [{"type": "function", "function": {
            "name": "read", "parameters": {"type": "object"},
        }}]
        actf, sft, usage = self._project(
            [_user(), provider, observation, _reply("cli", "final answer")],
            tools=tools,
        )
        self.assertEqual(len(actf["steps"]), 2)
        self.assertEqual(
            [message["role"] for message in sft["messages"]],
            ["system", "user", "assistant", "tool", "assistant"],
        )
        self.assertEqual(sft["messages"][-1]["content"], "final answer")
        self.assertEqual(usage["request_count"], 1)
        self.assertEqual(usage["total_tokens"], 16)

    def test_different_or_missing_source_identity_is_not_deduped(self) -> None:
        for fields in (
            {"provider": "another"}, {"model": "another"},
            {"provider": None}, {"model": None},
        ):
            with self.subTest(fields=fields):
                actf, _, usage = self._project([
                    _user(), _reply("openai-completions"), _reply("cli", **fields),
                ])
                self.assertEqual(len(actf["steps"]), 2)
                self.assertEqual(usage["request_count"], 2)

    def test_identical_text_in_new_user_turn_is_not_a_copy(self) -> None:
        actf, sft, usage = self._project([
            _user("first"), _reply("openai-completions"),
            _user("second"), _reply("cli"),
        ])
        self.assertEqual(len(actf["steps"]), 2)
        self.assertEqual([m["content"] for m in sft["messages"] if m["role"] == "user"], ["first", "second"])
        self.assertEqual(usage["request_count"], 2)

    def test_session_and_compaction_boundaries_clear_matching_history(self) -> None:
        for kind in ("session", "compaction"):
            with self.subTest(kind=kind):
                actf, _, usage = self._project([
                    _user(), _reply("openai-completions"),
                    {"type": kind}, _reply("cli"),
                ])
                self.assertEqual(len(actf["steps"]), 2)
                self.assertEqual(usage["request_count"], 2)

    def test_independent_cli_or_provider_replies_are_not_deduped(self) -> None:
        for api in ("cli", "openai-completions"):
            with self.subTest(api=api):
                actf, _, usage = self._project([_user(), _reply(api), _reply(api)])
                self.assertEqual(len(actf["steps"]), 2)
                self.assertEqual(usage["request_count"], 2)

    def test_unmatched_cli_usage_is_preserved_even_when_text_matches(self) -> None:
        actf, _, usage = self._project([
            _user(), _reply("openai-completions"),
            _reply("cli", usage={"input": 20, "output": 4, "totalTokens": 24}),
        ])
        self.assertEqual(len(actf["steps"]), 2)
        self.assertEqual(usage["request_count"], 2)
        self.assertEqual(usage["total_tokens"], 40)

    def test_missing_provider_usage_does_not_erase_cli_accounting(self) -> None:
        actf, _, usage = self._project([
            _user(), _reply("openai-completions", usage=None), _reply("cli"),
        ])
        self.assertEqual(len(actf["steps"]), 2)
        self.assertEqual(usage["request_count"], 1)
        self.assertEqual(usage["total_tokens"], 16)
        self.assertEqual(actf["steps"][-1]["metric"]["prompt_tokens_len"], 10)

    def test_changed_cache_counts_are_not_a_usage_match(self) -> None:
        actf, _, usage = self._project([
            _user(), _reply("openai-completions"),
            _reply("cli", usage={"input": 10, "output": 2, "cacheRead": 1, "cacheWrite": 3, "totalTokens": 16}),
        ])
        self.assertEqual(len(actf["steps"]), 2)
        self.assertEqual(usage["request_count"], 2)
        self.assertEqual(usage["cache_write_tokens"], 4)

    def test_cli_new_reasoning_is_preserved(self) -> None:
        cli = _reply("cli")
        cli["message"]["content"].insert(0, {"type": "thinking", "thinking": "new reasoning"})
        actf, sft, usage = self._project([_user(), _reply("openai-completions"), cli])
        self.assertEqual(len(actf["steps"]), 2)
        self.assertEqual(sft["messages"][-1]["reasoning_content"], "new reasoning")
        self.assertEqual(usage["request_count"], 2)

    def test_cli_fallback_after_provider_failure_is_counted(self) -> None:
        for reason in ("error", "aborted"):
            with self.subTest(reason=reason):
                actf, _, usage = self._project([
                    _user(), _reply("openai-completions", stopReason=reason),
                    _reply("cli"),
                ])
                self.assertEqual(len(actf["steps"]), 2)
                self.assertEqual(usage["request_count"], 2)

    def test_known_cli_copy_without_usage_can_be_removed(self) -> None:
        actf, _, usage = self._project([
            _user(), _reply("openai-completions"), _reply("cli", usage=None),
        ])
        self.assertEqual(len(actf["steps"]), 1)
        self.assertEqual(usage["request_count"], 1)

    def test_repeated_mirrors_do_not_recount_the_provider(self) -> None:
        actf, _, usage = self._project([
            _user(), _reply("openai-completions"), _reply("cli"), _reply("cli"),
        ])
        self.assertEqual(len(actf["steps"]), 1)
        self.assertEqual(usage["request_count"], 1)

    def test_matching_an_earlier_reply_does_not_erase_new_final_text(self) -> None:
        actf, sft, usage = self._project([
            _user(), _reply("openai-completions", "earlier"),
            _reply("openai-completions", "later"), _reply("cli", "earlier"),
        ])
        self.assertEqual(len(actf["steps"]), 3)
        self.assertEqual(sft["messages"][-1]["content"], "earlier")
        self.assertEqual(usage["request_count"], 2)

    def test_null_total_tokens_is_normalized_like_missing(self) -> None:
        for provider_total, cli_total in ((None, 16), (16, None), (None, None)):
            with self.subTest(provider=provider_total, cli=cli_total):
                actf, _, usage = self._project([
                    _user(),
                    _reply("openai-completions", usage={
                        "input": 10, "output": 2, "cacheRead": 3,
                        "cacheWrite": 1, "totalTokens": provider_total,
                        "cost": {"total": 0.05},
                    }),
                    _reply("cli", usage={
                        "input": 10, "output": 2, "cacheRead": 3,
                        "cacheWrite": 1, "totalTokens": cli_total,
                        "cost": {"total": 0.05},
                    }),
                ])
                self.assertEqual(len(actf["steps"]), 1)
                self.assertEqual(usage["request_count"], 1)
                self.assertEqual(usage["total_tokens"], 16)
                self.assertEqual(usage["cost_usd"], 0.05)

    def test_mirror_cost_unrecorded_by_provider_is_preserved(self) -> None:
        actf, _, usage = self._project([
            _user(),
            _reply("openai-completions", usage={
                "input": 10, "output": 2, "cacheRead": 3,
                "cacheWrite": 1, "totalTokens": 16,
            }),
            _reply("cli", usage={
                "input": 10, "output": 2, "cacheRead": 3,
                "cacheWrite": 1, "totalTokens": 16, "cost": {"total": 0.05},
            }),
        ])
        self.assertEqual(len(actf["steps"]), 1)
        self.assertEqual(usage["request_count"], 1)
        self.assertEqual(usage["total_tokens"], 16)
        self.assertEqual(usage["cost_usd"], 0.05)

    def test_turn_total_mirror_cost_accounting(self) -> None:
        for cli_cost, expected in ((None, 0.0), ({"total": 0.1}, 0.1), ({"total": 0.03}, 0.03)):
            with self.subTest(cli_cost=cli_cost):
                cli_usage = {
                    "input": 20, "output": 4, "cacheRead": 6,
                    "cacheWrite": 2, "totalTokens": 32,
                }
                if cli_cost is not None:
                    cli_usage["cost"] = cli_cost
                actf, _, usage = self._project([
                    _user(),
                    _reply("openai-completions", "first", usage={
                        "input": 10, "output": 2, "cacheRead": 3,
                        "cacheWrite": 1, "totalTokens": 16,
                    }),
                    _reply("openai-completions", "second", usage={
                        "input": 10, "output": 2, "cacheRead": 3,
                        "cacheWrite": 1, "totalTokens": 16,
                    }),
                    _reply("cli", "second", usage=cli_usage),
                ])
                self.assertEqual(len(actf["steps"]), 2)
                self.assertEqual(usage["request_count"], 2)
                self.assertEqual(usage["total_tokens"], 32)
                self.assertEqual(usage["cost_usd"], expected)

    def test_turn_total_cost_already_recorded_is_not_counted_twice(self) -> None:
        actf, _, usage = self._project([
            _user(),
            _reply("openai-completions", "first"),
            _reply("openai-completions", "second"),
            _reply("cli", "second", usage={
                "input": 20, "output": 4, "cacheRead": 6,
                "cacheWrite": 2, "totalTokens": 32, "cost": {"total": 0.1},
            }),
        ])
        self.assertEqual(len(actf["steps"]), 2)
        self.assertEqual(usage["request_count"], 2)
        self.assertEqual(usage["total_tokens"], 32)
        self.assertEqual(usage["cost_usd"], 0.1)


if __name__ == "__main__":
    unittest.main()
