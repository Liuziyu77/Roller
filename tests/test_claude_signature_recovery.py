from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from roller.claude_signature_recovery import (
    extract_signature_candidates,
    recover_claude_signatures,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def _trace(path: Path, *, native_reasoning: str = "native summary") -> None:
    rows = [
        {
            "type": "user",
            "uuid": "user-1",
            "timestamp": "2026-08-22T01:00:00Z",
            "message": {"role": "user", "content": "solve the task"},
        },
        {
            "type": "assistant",
            "uuid": "assistant-thinking",
            "timestamp": "2026-08-22T01:00:01Z",
            "message": {
                "id": "msg-1",
                "model": "claude-opus-4-8",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": native_reasoning,
                        "signature": "opaque-signature",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "uuid": "assistant-text",
            "timestamp": "2026-08-22T01:00:02Z",
            "message": {
                "id": "msg-1",
                "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": "42"}],
            },
        },
    ]
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


class ClaudeSignatureRecoveryTests(unittest.TestCase):
    def test_extracts_signature_and_groups_split_message_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trace = Path(temporary) / "trace.jsonl"
            _trace(trace)
            candidates = extract_signature_candidates(trace)

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.message_id, "msg-1")
        self.assertEqual(candidate.model, "claude-opus-4-8")
        self.assertEqual(candidate.signature, "opaque-signature")
        self.assertEqual(candidate.native_reasoning, "native summary")
        self.assertEqual(candidate.visible_text, "42")
        self.assertEqual(candidate.preceding_user, "solve the task")

    @patch("roller.claude_signature_recovery.urllib_request.urlopen")
    def test_recovers_without_persisting_duplicate_signature(self, urlopen) -> None:
        urlopen.return_value = _Response(
            {
                "content": [
                    {
                        "type": "text",
                        "text": "<cot>full recovered working with several concrete steps</cot>",
                    }
                ],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 4,
                    "cache_creation_input_tokens": 5,
                    "cache_read_input_tokens": 6,
                },
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace = root / "trace.jsonl"
            destination = root / "recovery.jsonl"
            _trace(trace)
            summary = recover_claude_signatures(
                trace,
                destination,
                api_base="http://example.test/v1/messages",
                api_key="secret-key",
                default_model="fallback",
                max_tokens=1234,
            )
            record_text = destination.read_text(encoding="utf-8")
            record = json.loads(record_text)

        request = urlopen.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "http://example.test/v1/messages")
        self.assertEqual(body["model"], "claude-opus-4-8")
        self.assertEqual(body["max_tokens"], 1234)
        self.assertEqual(
            body["messages"][1]["content"][0]["signature"], "opaque-signature"
        )
        recovered = "full recovered working with several concrete steps"
        self.assertEqual(summary.recovered_by_message, {"msg-1": recovered})
        self.assertEqual(summary.usage["total_tokens"], 18)
        self.assertEqual(summary.usage["request_count"], 1)
        self.assertEqual(record["quality"], "rewrite_or_unverified")
        self.assertEqual(record["recovered_reasoning"], recovered)
        self.assertNotIn("opaque-signature", record_text)
        self.assertNotIn("secret-key", record_text)

    @patch("roller.claude_signature_recovery.urllib_request.urlopen")
    def test_retries_a_refusal_and_keeps_success(self, urlopen) -> None:
        urlopen.side_effect = [
            _Response(
                {
                    "content": [{"type": "text", "text": "I cannot share private work"}],
                    "usage": {"output_tokens": 2},
                }
            ),
            _Response(
                {
                    "content": [{"type": "text", "text": "<cot>native summary</cot>"}],
                    "stop_reason": "end_turn",
                    "usage": {"output_tokens": 3},
                }
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace = root / "trace.jsonl"
            _trace(trace)
            summary = recover_claude_signatures(
                trace,
                root / "recovery.jsonl",
                api_base="http://example.test",
                api_key="secret-key",
                default_model="fallback",
                attempts=2,
            )

        self.assertEqual(summary.recovered_by_message["msg-1"], "native summary")
        self.assertEqual(summary.records[0]["quality"], "exact")
        self.assertEqual(
            [row["status"] for row in summary.records[0]["attempts"]],
            ["refusal", "ok"],
        )
        self.assertEqual(summary.usage["request_count"], 2)

    @patch("roller.claude_signature_recovery.urllib_request.urlopen")
    def test_rejects_missing_prior_thinking_response(self, urlopen) -> None:
        urlopen.return_value = _Response(
            {
                "content": [
                    {
                        "type": "text",
                        "text": "There is no prior thinking block to copy verbatim.",
                    }
                ],
                "usage": {"output_tokens": 8},
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace = root / "trace.jsonl"
            _trace(trace, native_reasoning="")
            summary = recover_claude_signatures(
                trace,
                root / "recovery.jsonl",
                api_base="http://example.test",
                api_key="secret-key",
                default_model="fallback",
            )

        self.assertEqual(summary.recovered_by_message, {})
        self.assertEqual(summary.records[0]["status"], "refusal")
        self.assertEqual(summary.records[0]["attempts"][0]["status"], "refusal")

    @patch("roller.claude_signature_recovery.urllib_request.urlopen")
    def test_fill_mode_skips_existing_native_reasoning_without_request(self, urlopen) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace = root / "trace.jsonl"
            _trace(trace)
            summary = recover_claude_signatures(
                trace,
                root / "recovery.jsonl",
                api_base="http://example.test",
                api_key="secret-key",
                default_model="fallback",
                only_missing=True,
            )

        urlopen.assert_not_called()
        self.assertEqual(summary.recovered_by_message, {})
        self.assertEqual(summary.records[0]["status"], "skipped_native")
        self.assertEqual(summary.usage["request_count"], 0)

    @patch("roller.claude_signature_recovery.urllib_request.urlopen")
    def test_rejects_visible_answer_as_insufficient_recovery(self, urlopen) -> None:
        urlopen.return_value = _Response(
            {
                "content": [{"type": "text", "text": "42"}],
                "usage": {"output_tokens": 1},
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace = root / "trace.jsonl"
            _trace(trace, native_reasoning="")
            summary = recover_claude_signatures(
                trace,
                root / "recovery.jsonl",
                api_base="http://example.test",
                api_key="secret-key",
                default_model="fallback",
            )

        self.assertEqual(summary.recovered_by_message, {})
        self.assertEqual(summary.records[0]["attempts"][0]["status"], "insufficient")

    @patch("roller.claude_signature_recovery.urllib_request.urlopen")
    def test_redacts_signature_and_api_key_from_recorded_errors(self, urlopen) -> None:
        urlopen.side_effect = RuntimeError("opaque-signature secret-key")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace = root / "trace.jsonl"
            _trace(trace)
            destination = root / "recovery.jsonl"
            recover_claude_signatures(
                trace,
                destination,
                api_base="http://example.test",
                api_key="secret-key",
                default_model="fallback",
            )
            record_text = destination.read_text(encoding="utf-8")

        self.assertNotIn("opaque-signature", record_text)
        self.assertNotIn("secret-key", record_text)
        self.assertIn("<redacted-signature>", record_text)
        self.assertIn("<redacted-api-key>", record_text)


if __name__ == "__main__":
    unittest.main()
