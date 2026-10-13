from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from roller.benches.transcripts import session_trajectory


class BenchTranscriptTests(unittest.TestCase):
    def test_openclaw_conversion_receives_messages_and_keep_thinking(self) -> None:
        converter = Mock(return_value={"steps": []})
        harness = SimpleNamespace(
            name="openclaw",
            config=SimpleNamespace(keep_thinking=False),
            _collect_trace=Mock(return_value=Path("/tmp/native.jsonl")),
        )
        session = SimpleNamespace(
            harness=harness,
            container="container",
            output_dir=Path("/tmp/output"),
            attempt=1,
            messages=["task prompt"],
            state_dir="/tmp/state",
        )

        with patch(
            "roller.benches.transcripts.importlib.import_module",
            return_value=SimpleNamespace(convert_openclaw_trace=converter),
        ):
            session_trajectory(session)

        self.assertEqual(converter.call_args.kwargs["user_messages"], ["task prompt"])
        self.assertFalse(converter.call_args.kwargs["keep_thinking"])

    def test_claude_conversion_receives_messages_and_keep_thinking(self) -> None:
        converter = Mock(return_value={"steps": []})
        harness = SimpleNamespace(
            name="claude-code",
            config=SimpleNamespace(keep_thinking=False),
            _collect_trace=Mock(return_value=Path("/tmp/native.jsonl")),
        )
        session = SimpleNamespace(
            harness=harness,
            container="container",
            output_dir=Path("/tmp/output"),
            attempt=1,
            messages=["task prompt"],
        )

        with patch(
            "roller.benches.transcripts.importlib.import_module",
            return_value=SimpleNamespace(convert_claude_code_trace=converter),
        ):
            session_trajectory(session)

        self.assertEqual(converter.call_args.kwargs["user_messages"], ["task prompt"])
        self.assertFalse(converter.call_args.kwargs["keep_thinking"])


if __name__ == "__main__":
    unittest.main()
