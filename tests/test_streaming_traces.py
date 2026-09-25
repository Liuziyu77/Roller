from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from roller.pi_actf import read_pi_events
from roller.redaction import redact_file_literals


class StreamingTraceTests(unittest.TestCase):
    def test_redacts_secrets_at_every_chunk_boundary_and_across_newlines(self):
        content = "begin\r\nalpha\nbeta\u2028end" * 10
        for chunk_size in (1, 2, 3, 8, 64):
            with self.subTest(chunk_size=chunk_size), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "trace"
                path.write_bytes(content.encode())
                with patch.object(Path, "read_text", side_effect=AssertionError("whole file read")):
                    redact_file_literals(path, ["alpha\nbeta"], chunk_size=chunk_size)
                self.assertEqual(path.read_bytes().decode(), content.replace("alpha\nbeta", "[REDACTED]"))
                self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_literal_metacharacters_and_multiple_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace"
            path.write_text("a.b|token+value|a.b", encoding="utf-8")
            redact_file_literals(path, ["a.b", "token+value", ""], chunk_size=2)
            self.assertEqual(path.read_text(), "[REDACTED]|[REDACTED]|[REDACTED]")

    def test_decode_failure_keeps_original_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace"
            path.write_bytes(b"key\xff")
            with self.assertRaises(UnicodeDecodeError):
                redact_file_literals(path, ["key"], chunk_size=1)
            self.assertEqual(path.read_bytes(), b"key\xff")
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_pi_reader_keeps_envelopes_without_partial_snapshots(self):
        rows = [
            {"type": "turn_start", "turn": 1},
            {"type": "event", "turn": 1, "event": {"type": "message_update", "text": "x" * 10000}},
            {"type": "event", "turn": 1, "event": {"type": "message_end", "message": {"content": "a\u2028b\u2029c"}}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            raw = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
            path.write_text(raw, encoding="utf-8")
            with patch.object(Path, "read_text", side_effect=AssertionError("whole file read")):
                events = read_pi_events(path)
            self.assertEqual(events[1], {"type": "event", "turn": 1, "event": {"type": "message_update"}})
            self.assertEqual(events[0], rows[0])
            self.assertEqual(events[2], rows[2])
            self.assertEqual(path.read_text(), raw)

    def test_pi_reader_rejects_non_object_and_malformed_rows(self):
        for row in ("[]", "{truncated"):
            with self.subTest(row=row), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "trace"
                path.write_text(row, encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_pi_events(path)
