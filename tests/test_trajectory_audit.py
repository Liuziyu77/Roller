import json
from pathlib import Path
import tempfile
import unittest

from roller.trajectory_audit import (
    completion_audit, record_training_audits, wire_effort_audit,
    sft_exclusion_reason, training_summary,
    native_completion_audit,
)


class TrajectoryAuditTests(unittest.TestCase):
    def test_unified_visible_reasoning_audit_excludes_simulator(self):
        with tempfile.TemporaryDirectory() as temporary:
            metadata = {"reasoning_effort": "off", "keep_thinking": True}
            actf = {"steps": [{"assistant_content": {
                "content": "answer", "reasoning_content": "visible reasoning"},
                "metric": {"stop_reason": "stop"}}]}
            record_training_audits(metadata, actf, Path(temporary) / "wire_trace.jsonl")
            self.assertEqual(metadata["thinking_audit"]["status"], "mismatch")
            self.assertEqual(metadata["sft_eligibility"]["reason"], "thinking_effort_mismatch")

    def test_final_response_truncation_overrides_actf_stop_and_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "wire_trace.jsonl"
            actf = {"steps": [{"assistant_content": {"content": "answer"},
                                 "metric": {"stop_reason": "stop"}}]}
            rows = [
                {"seq": 1, "path": "/v1/responses", "model": "model", "body": {}},
                {"kind": "response_metadata", "request_seq": 1, "response_models": ["model"],
                 "finish_reasons": ["max_output_tokens"], "response_statuses": ["incomplete"],
                 "capture_status": "observed"},
            ]
            path.write_text("\n".join(map(json.dumps, rows)) + "\n")
            metadata = {}
            record_training_audits(metadata, actf, path)
            self.assertEqual(metadata["completion_audit"]["status"], "incomplete")
            self.assertEqual(metadata["actf_completion_audit"]["status"], "not_flagged")
            self.assertEqual(metadata["response_completion_audit"]["status"], "incomplete")
            self.assertEqual(metadata["sft_eligibility"]["reason"], "token_limit")
            rows.extend([
                {"seq": 2, "path": "/v1/responses", "model": "model", "body": {}},
                {"kind": "response_metadata", "request_seq": 2, "response_models": ["model"],
                 "finish_reasons": [], "response_statuses": ["completed"],
                 "capture_status": "observed"},
            ])
            path.write_text("\n".join(map(json.dumps, rows)) + "\n")
            recovered = {}
            record_training_audits(recovered, actf, path)
            self.assertEqual(recovered["response_completion_audit"]["status"], "not_flagged")
            self.assertNotIn("sft_eligibility", recovered)

    def test_native_request_mismatch_is_used_without_wire_trace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "trace.jsonl").write_text(json.dumps({"type": "request", "turn": 1,
                "body": {"reasoning_effort": "high"}}) + "\n")
            metadata = {"reasoning_effort": "low", "raw_trace_schema": "roller.pi-events/v1"}
            record_training_audits(metadata, {"steps": [{"assistant_content": {"content": "ok"}}]},
                                   root / "wire_trace.jsonl")
            self.assertEqual(metadata["wire_effort_audit"]["status"], "mismatch")
            self.assertEqual(metadata["wire_effort_audit"]["source"], "native")
            self.assertEqual(metadata["sft_eligibility"]["reason"], "wire_effort_mismatch")

    def test_claude_ultra_keeps_logical_effort_and_matches_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "wire_trace.jsonl"
            path.write_text(json.dumps({"path": "/v1/messages", "body": {
                "output_config": {"effort": "ultracode"}}}) + "\n")
            metadata = {"reasoning_effort": "ultra", "transport_effort": "ultracode"}
            record_training_audits(metadata, None, path)
            self.assertEqual(metadata["wire_effort_audit"]["requested_effort"], "ultra")
            self.assertEqual(metadata["wire_effort_audit"]["status"], "matched")

    def test_openclaw_native_completion_ignores_user_and_preserves_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trace.jsonl"
            for content, reason, expected in (
                ([{"type": "thinking", "thinking": "only reasoning"}], "stop", "incomplete"),
                ([{"type": "toolCall", "id": "a"}], "toolUse", "not_flagged"),
                ([{"type": "image", "data": "opaque"}], "stop", "not_flagged"),
                ([{"type": "text", "text": "partial"}], "length", "incomplete"),
            ):
                with self.subTest(reason=reason, content=content):
                    path.write_text(json.dumps({"type": "message", "message": {
                        "role": "assistant", "content": content, "stopReason": reason,
                    }}) + '\n' + json.dumps({"type": "message", "message": {"role": "user", "content": "next"}}) + '\n')
                    self.assertEqual(native_completion_audit(path, "openclaw-session-jsonl")["status"], expected)

    def test_native_truncation_without_actf_and_successful_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trace.jsonl"
            message = {"role": "assistant", "stopReason": "length", "content": [{"type": "thinking", "thinking": "partial"}]}
            row = {"type": "event", "event": {"type": "message_end", "message": message}}
            path.write_text(json.dumps(row) + "\n")
            audit = native_completion_audit(path, "roller.pi-events/v1")
            self.assertEqual(audit, {"status": "incomplete", "reason": "token_limit", "stop_reason": "length", "source": "native"})
            metadata = {"raw_trace_schema": "roller.pi-events/v1"}
            record_training_audits(metadata, None, path.with_name("wire_trace.jsonl"))
            self.assertEqual(metadata["sft_eligibility"]["reason"], "token_limit")
            # Request history and incomplete deltas must not replace last completed message.
            with path.open("a") as stream:
                stream.write(json.dumps({"type": "context", "context": {"messages": [{"role": "assistant", "stopReason": "stop"}]}}) + "\n")
            self.assertEqual(native_completion_audit(path, "roller.pi-events/v1")["status"], "incomplete")
            message.update(stopReason="stop", content=[{"type": "text", "text": "recovered"}])
            with path.open("a") as stream:
                stream.write(json.dumps(row) + "\n")
            self.assertEqual(native_completion_audit(path, "roller.pi-events/v1")["status"], "not_flagged")
            self.assertEqual(native_completion_audit(path, "unknown")["status"], "not_observable")
            path.write_text("{broken\n")
            self.assertEqual(native_completion_audit(path, "roller.pi-events/v1")["status"], "not_observable")

    def test_shared_summary_policy_and_overlapping_audits(self):
        results = [
            {"status": "ok"},
            {"status": "skipped", "runtime": {
                "thinking_audit": {"status": "mismatch"},
                "wire_effort_audit": {"status": "unverified"},
            }},
            {"status": "ok", "runtime": {
                "sft_eligibility": {"eligible": False, "reason": "wire_effort_mismatch"},
                "thinking_audit": {"status": "mismatch"},
                "wire_effort_audit": {"status": "mismatch"},
            }},
            {"status": "error"},
            {"status": "timeout"},
            {"status": "ok", "runtime": {
                "trajectory_conversion": {"status": "unavailable", "target": "sft", "error": "unsupported"},
            }},
            {"status": "ok", "runtime": {"wire_effort_audit": {"status": "not_observable"}}},
        ]
        before = json.dumps(results)
        self.assertEqual(training_summary(results), {
            "sft_included": 2, "sft_excluded": 5, "thinking_mismatch": 2,
            "wire_unverified": 1, "wire_not_observable": 1,
        })
        self.assertEqual(sum(sft_exclusion_reason(r) is None for r in results), 2)
        self.assertEqual(json.dumps(results), before)
        self.assertTrue(all(v == 0 for v in training_summary([]).values()))

    def test_wire_protocols_and_conservative_unknowns(self):
        cases = [
            ("off", {"reasoning_effort": "none"}, "matched"),
            ("off", {"thinking": {"type": "disabled"}}, "matched"),
            ("off", {"thinking": {"type": "enabled"}}, "mismatch"),
            ("off", {"chat_template_kwargs": {"enable_thinking": True}}, "mismatch"),
            ("low", {"reasoning": {"effort": "high"}}, "mismatch"),
            ("high", {"output_config": {"effort": "high"}}, "matched"),
            ("low", {"thinking": {"budget_tokens": 1024}}, "unverified"),
            ("off", {}, "unverified"),
            ("low", {"reasoning_effort": ["low"]}, "unverified"),
            ("low", {"thinking": {"type": []}}, "unverified"),
            ("off", {"reasoning_effort": "none", "enable_thinking": True}, "mismatch"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "wire.jsonl"
            for effort, body, expected in cases:
                for field in ("body", "params"):
                    with self.subTest(effort=effort, body=body, field=field):
                        path.write_text(json.dumps({"path": "/v1/chat/completions", field: body}) + "\n")
                        self.assertEqual(wire_effort_audit(path, effort)["status"], expected)

    def test_retries_missing_and_malformed_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "wire.jsonl"
            self.assertEqual(wire_effort_audit(path, "low")["status"], "not_observable")
            self.assertIsNone(wire_effort_audit(path, None))
            matched = json.dumps({"path": "/v1/responses", "body": {"reasoning": {"effort": "low"}}})
            mismatch = json.dumps({"path": "/v1/responses", "body": {"reasoning": {"effort": "high"}}})
            path.write_text(matched + "\n" + mismatch + "\n")
            audit = wire_effort_audit(path, "low")
            self.assertEqual(audit["status"], "mismatch")
            self.assertEqual(len(audit["requests"]), 2)
            path.write_text(matched + "\n{bad\n")
            self.assertEqual(wire_effort_audit(path, "low")["status"], "unverified")

    def test_completion_boundaries(self):
        for content, calls, stop, expected in (
            ("partial", [], "length", "incomplete"),
            ("partial", [], "max_tokens", "incomplete"),
            ("", [], "stop", "incomplete"),
            ("  ", [], "stop", "incomplete"),
            ("", [{"id": "a"}], "tool_use", "not_flagged"),
            ("answer", [], "stop", "not_flagged"),
            ([{"type": "image"}], [], "stop", "not_flagged"),
        ):
            with self.subTest(content=content, stop=stop):
                actf = {"steps": [{"assistant_content": {"content": content, "tool_calls": calls}, "metric": {"stop_reason": stop}}]}
                before = json.dumps(actf)
                self.assertEqual(completion_audit(actf)["status"], expected)
                self.assertEqual(json.dumps(actf), before)
        self.assertEqual(completion_audit({})["status"], "not_observable")
        # A successful continuation may recover an earlier token limit.
        self.assertEqual(completion_audit({"steps": [
            {"metric": {"stop_reason": "length"}},
            {"assistant_content": {"content": "recovered"}, "metric": {"stop_reason": "stop"}},
        ]})["status"], "not_flagged")

    def test_preserves_existing_exclusion_reason(self):
        metadata = {"sft_eligibility": {"eligible": False, "reason": "thinking_effort_mismatch"}}
        with tempfile.TemporaryDirectory() as temporary:
            record_training_audits(metadata, {"steps": [{"metric": {"stop_reason": "length"}}]}, Path(temporary) / "missing")
        self.assertEqual(metadata["sft_eligibility"]["reasons"], ["thinking_effort_mismatch", "token_limit"])
