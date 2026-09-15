from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from itertools import permutations
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


def _priced_reply(
    api: str, cost: float | None, *, calls: int = 1, **fields
) -> dict:
    reply = _reply(api, **fields)
    usage = reply["message"]["usage"]
    for key in ("input", "output", "cacheRead", "cacheWrite", "totalTokens"):
        usage[key] *= calls
    if cost is None:
        usage.pop("cost")
    else:
        usage["cost"]["total"] = cost
    return reply


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

    def test_cumulative_components_allow_last_call_total(self) -> None:
        # OpenClaw 2026.7.1 buildUsageAgentMetaFields keeps cumulative input,
        # output and cache fields, but replaces total with lastTurnTotal.
        # persistCliTurnTranscript writes that mixed snapshot unchanged.
        for total in (32, 48):
            for text, steps in (("second", 2), ("final answer", 3)):
                with self.subTest(total=total, text=text):
                    mirror = _priced_reply("cli", None, calls=3, text=text)
                    mirror["message"]["usage"]["totalTokens"] = total
                    actf, sft, usage = self._project([
                        _user(), _priced_reply("openai-completions", 0.05),
                        _priced_reply("openai-completions", 0.1, calls=2, text="second"),
                        mirror,
                    ])
                    self.assertEqual(usage, {
                        "input_tokens": 30, "output_tokens": 6,
                        "cache_read_tokens": 9, "cache_write_tokens": 3,
                        "total_tokens": 48, "cost_usd": 0.15, "request_count": 2,
                    })
                    self.assertEqual(len(actf["steps"]), steps)
                    self.assertEqual(sft["messages"][-1]["content"], text)
                    self.assertEqual(sum(
                        step["metric"]["prompt_tokens_len"] for step in actf["steps"]
                    ), 30)
                    self.assertEqual(sum(
                        step["metric"]["completion_tokens_len"] for step in actf["steps"]
                    ), 6)

    def test_cumulative_match_still_requires_valid_total_and_all_components(self) -> None:
        for changes in (
            {"totalTokens": 16},  # Earlier call, not latest or accumulated.
            {"totalTokens": 47},
            {"totalTokens": 0},
            {"totalTokens": float("nan")},
            {"cacheRead": 8, "cacheWrite": 4},  # Same sum, different fields.
            {"input": 29},
        ):
            with self.subTest(changes=changes):
                mirror = _priced_reply("cli", None, calls=3)
                mirror["message"]["usage"].update(totalTokens=32)
                mirror["message"]["usage"].update(changes)
                actf, _, usage = self._project([
                    _user(), _priced_reply("openai-completions", None),
                    _priced_reply("openai-completions", None, calls=2), mirror,
                ])
                self.assertEqual(usage["request_count"], 3)
                self.assertEqual(len(actf["steps"]), 3)

    def test_retried_turn_keeps_failed_usage_in_accounting_history(self) -> None:
        for reason in ("error", "aborted"):
            for reason_key in ("stopReason", "stop_reason"):
                for total in (16, 48):
                    with self.subTest(reason=reason, reason_key=reason_key, total=total):
                        failed = _priced_reply("openai-completions", 0.02)
                        failed["message"].pop("stopReason")
                        failed["message"][reason_key] = reason
                        mirror = _priced_reply("cli", 0.15, calls=3)
                        mirror["message"]["usage"]["totalTokens"] = total
                        actf, _, usage = self._project([
                            _user(), _priced_reply("openai-completions", None),
                            _priced_reply("cli", 0.05), failed,
                            _priced_reply("openai-completions", None),
                            mirror, mirror, _priced_reply("cli", 0.08),
                        ])
                        self.assertEqual(usage, {
                            "input_tokens": 30, "output_tokens": 6,
                            "cache_read_tokens": 9, "cache_write_tokens": 3,
                            "total_tokens": 48, "cost_usd": 0.15, "request_count": 3,
                        })
                        self.assertEqual(len(actf["steps"]), 3)

    def test_failed_latest_call_cannot_absorb_cumulative_cli_fallback(self) -> None:
        for total in (16, 32):
            with self.subTest(total=total):
                fallback = _priced_reply("cli", 0.1, calls=2)
                fallback["message"]["usage"]["totalTokens"] = total
                actf, _, usage = self._project([
                    _user(), _reply("openai-completions"),
                    _reply("openai-completions", stopReason="error"), fallback,
                ])
                self.assertEqual(usage["request_count"], 3)
                self.assertEqual(usage["cost_usd"], 0.2)
                self.assertEqual(usage["total_tokens"], 32 + total)
                self.assertEqual(len(actf["steps"]), 3)

    def test_retry_aggregate_requires_complete_valid_accounting_history(self) -> None:
        for failed_usage in (None, {"input": -1}):
            with self.subTest(failed_usage=failed_usage):
                mirror = _priced_reply("cli", None, calls=2)
                mirror["message"]["usage"]["totalTokens"] = 16
                actf, _, usage = self._project([
                    _user(), _reply("openai-completions"),
                    _reply("openai-completions", stopReason="error", usage=failed_usage),
                    _reply("openai-completions"), mirror,
                ])
                self.assertEqual(len(actf["steps"]), 4)
                self.assertEqual(usage["input_tokens"], 40)
                self.assertEqual(usage["request_count"], 3 if failed_usage is None else 4)

    def test_context_total_override_requires_positive_latest_total(self) -> None:
        # Native buildUsageAgentMetaFields overrides total only when > 0.
        latest = _reply("openai-completions")
        latest["message"]["usage"]["totalTokens"] = 0
        mirror = _priced_reply("cli", None, calls=2)
        mirror["message"]["usage"]["totalTokens"] = 0
        actf, _, usage = self._project([_user(), _reply("openai-completions"), latest, mirror])
        self.assertEqual(usage["request_count"], 3)
        self.assertEqual(len(actf["steps"]), 3)

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

    def test_fallback_after_a_later_provider_failure_is_counted(self) -> None:
        # The failure boundary must keep an earlier successful call from
        # absorbing a fallback whose counters coincide with it.
        actf, _, usage = self._project([
            _user(),
            _reply("openai-completions"),
            _reply("openai-completions", stopReason="error"),
            _reply("cli"),
        ])
        self.assertEqual(len(actf["steps"]), 3)
        self.assertEqual(usage["request_count"], 3)
        self.assertEqual(usage["total_tokens"], 48)

        # A later success closes the boundary; delivery copies of it dedupe.
        actf, _, usage = self._project([
            _user(),
            _reply("openai-completions"),
            _reply("openai-completions", stopReason="error"),
            _reply("openai-completions", "done"),
            _reply("cli"),
        ])
        self.assertEqual(len(actf["steps"]), 3)
        self.assertEqual(usage["request_count"], 3)
        self.assertEqual(usage["total_tokens"], 48)

    def test_invalid_counters_cannot_break_or_contaminate_step_metrics(self) -> None:
        for invalid in (True, -1, "10", float("nan"), float("inf")):
            with self.subTest(invalid=invalid):
                provider = _reply("openai-completions", usage={
                    "input": invalid, "output": invalid, "totalTokens": invalid,
                })
                actf, _, usage = self._project([_user(), provider])
                self.assertEqual(len(actf["steps"]), 1)
                self.assertEqual(actf["steps"][0]["metric"]["prompt_tokens_len"], 0)
                self.assertEqual(actf["steps"][0]["metric"]["completion_tokens_len"], 0)
                self.assertEqual(usage["request_count"], 1)
                self.assertEqual(usage["total_tokens"], 0)

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

    def test_mirror_cost_reports_are_idempotent_and_monotonic(self) -> None:
        for provider_cost in (None, 0.02, 0.05):
            for cost_only in (False, True):
                with self.subTest(provider_cost=provider_cost, cost_only=cost_only):
                    mirrors = [
                        _priced_reply("cli", cost)
                        for cost in (0.05, 0.05, 0.03, 0.08, 0.08)
                    ]
                    if cost_only:
                        for mirror in mirrors:
                            mirror["message"]["usage"] = {
                                "cost": mirror["message"]["usage"]["cost"],
                            }
                    actf, _, usage = self._project([
                        _user(), _priced_reply("openai-completions", provider_cost), *mirrors,
                    ])
                    self.assertEqual(usage["cost_usd"], 0.08)
                    self.assertEqual(usage["total_tokens"], 16)
                    self.assertEqual(usage["request_count"], 1)
                    self.assertEqual(len(actf["steps"]), 1)

    def test_overlapping_cost_scopes_do_not_double_count(self) -> None:
        provider = _priced_reply("openai-completions", None)
        single = _priced_reply("cli", 0.05)
        aggregate = _priced_reply("cli", 0.1, calls=2)
        cases = {
            "single_then_aggregate": [provider, single, provider, aggregate, aggregate],
            "aggregate_then_single": [provider, provider, aggregate, single, aggregate],
            "singles_then_aggregate": [provider, single, provider, single, aggregate],
            "aggregate_then_cost_only": [
                provider, provider, aggregate,
                _reply("cli", usage={"cost": {"total": 0.05}}),
            ],
        }
        for name, events in cases.items():
            with self.subTest(name=name):
                actf, _, usage = self._project([_user(), *events])
                self.assertEqual(usage["cost_usd"], 0.1)
                self.assertEqual(usage["total_tokens"], 32)
                self.assertEqual(usage["request_count"], 2)
                self.assertEqual(len(actf["steps"]), 2)

    def test_cost_report_order_does_not_change_accounting(self) -> None:
        reports = (
            _priced_reply("cli", 0.05), _priced_reply("cli", 0.08),
            _priced_reply("cli", 0.12, calls=2),
            _priced_reply("cli", 0.1, calls=2),
        )
        for order in permutations(range(len(reports))):
            with self.subTest(order=order):
                _, _, usage = self._project([
                    _user(), _priced_reply("openai-completions", 0.07),
                    _priced_reply("openai-completions", None),
                    *(reports[index] for index in order),
                ])
                self.assertEqual(usage["cost_usd"], 0.15)
                self.assertEqual(usage["request_count"], 2)
                self.assertEqual(usage["total_tokens"], 32)

    def test_aggregate_cost_floor_does_not_absorb_new_call_cost(self) -> None:
        for new_call_cost, mirror_cost in ((0.05, 0.05), (None, 0.05)):
            with self.subTest(new_call_cost=new_call_cost):
                actf, _, usage = self._project([
                    _user(), _priced_reply("openai-completions", None),
                    _priced_reply("openai-completions", None),
                    _priced_reply("cli", 0.1, calls=2),
                    _priced_reply("openai-completions", new_call_cost),
                    _priced_reply("cli", mirror_cost),
                    _priced_reply("cli", 0.15, calls=3),
                    _priced_reply("cli", 0.15, calls=3),
                ])
                self.assertEqual(usage["cost_usd"], 0.15)
                self.assertEqual(usage["total_tokens"], 48)
                self.assertEqual(usage["request_count"], 3)
                self.assertEqual(len(actf["steps"]), 3)

    def test_cost_credits_are_scoped_to_source_and_turn(self) -> None:
        for boundary in (_user("next"), {"type": "session"}, {"type": "compaction"}):
            with self.subTest(boundary=boundary):
                _, _, usage = self._project([
                    _user(), _priced_reply("openai-completions", None),
                    _priced_reply("cli", 0.05), boundary,
                    _priced_reply("openai-completions", None), _priced_reply("cli", 0.05),
                ])
                self.assertEqual(usage["cost_usd"], 0.1)
                self.assertEqual(usage["request_count"], 2)
        for source in ({"provider": "other"}, {"model": "other"}):
            with self.subTest(source=source):
                _, _, usage = self._project([
                    _user(), _priced_reply("openai-completions", None),
                    _priced_reply("cli", 0.05),
                    _priced_reply("openai-completions", None, **source),
                    _priced_reply("cli", 0.07, **source),
                    _priced_reply("cli", 0.07, **source),
                ])
                self.assertEqual(usage["cost_usd"], 0.12)
                self.assertEqual(usage["request_count"], 2)

    def test_cost_only_mirror_requires_content_and_source_match(self) -> None:
        for fields in ({"text": "new answer"}, {"provider": "other"}):
            with self.subTest(fields=fields):
                actf, _, usage = self._project([
                    _user(), _priced_reply("openai-completions", 0.05),
                    _reply("cli", usage={"cost": {"total": 0.07}}, **fields),
                ])
                self.assertEqual(usage["cost_usd"], 0.12)
                self.assertEqual(usage["request_count"], 2)
                self.assertEqual(len(actf["steps"]), 2)

    def test_gap_fill_receives_only_incremental_cost_before_reset(self) -> None:
        actf, sft, usage = self._project([
            _user(), _priced_reply("openai-completions", None),
            _priced_reply("cli", 0.05), _priced_reply("cli", 0.08, text="final answer"),
        ])
        self.assertEqual(usage["cost_usd"], 0.08)
        self.assertEqual(usage["request_count"], 1)
        self.assertEqual(len(actf["steps"]), 2)
        self.assertEqual(sft["messages"][-1]["content"], "final answer")

    def test_invalid_counters_cannot_prove_a_duplicate(self) -> None:
        for invalid in (True, -1, "10", float("nan"), float("inf")):
            with self.subTest(invalid=invalid):
                provider = _priced_reply("openai-completions", 0.05)
                cli = _priced_reply("cli", 0.05)
                provider["message"]["usage"]["cacheRead"] = invalid
                cli["message"]["usage"]["cacheRead"] = invalid
                actf, _, usage = self._project([_user(), provider, cli])
                self.assertEqual(usage["request_count"], 2)
                self.assertEqual(usage["cache_read_tokens"], 0)
                self.assertEqual(len(actf["steps"]), 2)


if __name__ == "__main__":
    unittest.main()
