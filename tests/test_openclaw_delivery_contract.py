"""State-boundary tests, separate from individual delivery-format regressions."""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from roller.actf import openclaw_events_to_actf
from roller.sft import actf_to_sft
from roller.usage import extract_openclaw_usage


def reply(api="provider", text="done", *, calls=1, **fields):
    return {"type": "message", "message": {
        "role": "assistant", "api": api, "provider": "A", "model": "model",
        "stopReason": "stop", "content": [{"type": "text", "text": text}],
        "usage": {"input": 10 * calls, "output": 2 * calls,
                  "totalTokens": 12 * calls, "cost": {"total": 0.05 * calls}},
        **fields,
    }}


class DeliveryContractTests(unittest.TestCase):
    def project(self, events):
        events = [{"type": "message", "message": {"role": "user", "content": "hi"}}, *events]
        original = deepcopy(events)
        actf = openclaw_events_to_actf(events)
        sft = actf_to_sft(actf)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            raw = "".join(json.dumps(event) + "\n" for event in events)
            path.write_text(raw, encoding="utf-8")
            usage = extract_openclaw_usage(path)
            self.assertEqual(path.read_text(encoding="utf-8"), raw)
        self.assertEqual(events, original)
        return actf, sft, usage

    def test_late_cross_source_cli_never_uses_older_history(self):
        for changed in ({"provider": "B"}, {"model": "other"}):
            for cli_usage in (None, {"input": 10, "output": 2}, {"cost": {"total": 0.07}}):
                with self.subTest(changed=changed, cli_usage=cli_usage):
                    actf, _, usage = self.project([
                        reply(), reply(**changed), reply("cli", usage=cli_usage),
                    ])
                    self.assertEqual(len(actf["steps"]), 3)
                    self.assertEqual(usage["request_count"], 2 if cli_usage is None else 3)

    def test_gapfill_text_and_accounting_have_independent_lifetimes(self):
        for copies in (1, 2, 3):
            with self.subTest(copies=copies):
                actf, sft, usage = self.project([
                    reply(text="draft"), *[reply("cli", "final") for _ in range(copies)],
                    reply(text="second"), reply("cli", "second", calls=2),
                ])
                self.assertEqual(len(actf["steps"]), 3)
                self.assertEqual([m["content"] for m in sft["messages"] if m["role"] == "assistant"],
                                 ["draft", "final", "second"])
                self.assertEqual(usage["request_count"], 2)
                self.assertEqual(usage["total_tokens"], 24)
                self.assertEqual(usage["cost_usd"], 0.1)
                self.assertEqual(actf["steps"][1]["metric"]["prompt_tokens_len"], 0)

    def test_independent_cli_blocks_delivery_until_another_provider(self):
        independent = (
            reply("cli", provider="B"),
            reply("cli", calls=2),
            reply("cli", content=[{"type": "thinking", "thinking": "new reasoning"},
                                  {"type": "text", "text": "done"}]),
            reply("cli", stopReason="error"),
        )
        for event in independent:
            with self.subTest(event=event):
                # Even a later CLI whose text/counts equal the old provider is
                # independent: no new provider has restored eligibility.
                actf, _, usage = self.project([reply(), event, reply("cli")])
                self.assertEqual(len(actf["steps"]), 3)
                self.assertEqual(usage["request_count"], 3)

                # A provider restores eligibility without losing prior usage.
                actf, _, usage = self.project([
                    reply(), event, reply(), reply("cli", calls=2),
                ])
                self.assertEqual(len(actf["steps"]), 3)
                self.assertEqual(usage["request_count"], 3)

    def test_failure_fallback_shape_does_not_change_ledger_lifetime(self):
        for fallback in (
            reply("cli"),
            reply("cli", content=[{"type": "thinking", "thinking": "retry"},
                                  {"type": "text", "text": "done"}]),
        ):
            for reason in ("error", "aborted"):
                with self.subTest(fallback=fallback, reason=reason):
                    actf, _, usage = self.project([
                        reply(stopReason=reason), fallback, reply(), reply("cli", calls=2),
                    ])
                    self.assertEqual(len(actf["steps"]), 3)
                    self.assertEqual(usage["request_count"], 3)
                    self.assertEqual(usage["total_tokens"], 36)
                    self.assertEqual(usage["cost_usd"], 0.15)

    def test_true_turn_boundaries_clear_both_delivery_and_ledger(self):
        for boundary in (
            {"type": "message", "message": {"role": "user", "content": "next"}},
            {"type": "session"}, {"type": "compaction"},
        ):
            with self.subTest(boundary=boundary):
                actf, _, usage = self.project([
                    reply(), reply("cli", "gap fill"), boundary,
                    reply(), reply("cli", calls=2),
                ])
                self.assertEqual(len(actf["steps"]), 4)
                self.assertEqual(usage["request_count"], 3)
                self.assertEqual(usage["total_tokens"], 48)

    def test_legacy_usage_aliases_reach_metrics_and_matching(self):
        for provider_legacy, cli_legacy in ((True, True), (True, False), (False, True)):
            with self.subTest(provider_legacy=provider_legacy, cli_legacy=cli_legacy):
                events = [reply(), reply("cli")]
                for event, legacy in zip(events, (provider_legacy, cli_legacy)):
                    if legacy:
                        raw = event["message"]["usage"]
                        raw["input_tokens"] = raw.pop("input")
                        raw["output_tokens"] = raw.pop("output")
                actf, _, usage = self.project(events)
                self.assertEqual(len(actf["steps"]), 1)
                self.assertEqual(actf["steps"][0]["metric"]["prompt_tokens_len"], 10)
                self.assertEqual(actf["steps"][0]["metric"]["completion_tokens_len"], 2)
                self.assertEqual(usage["request_count"], 1)
                self.assertEqual(usage["total_tokens"], 12)


if __name__ == "__main__":
    unittest.main()
