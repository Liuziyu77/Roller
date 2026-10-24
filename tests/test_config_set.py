from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from roller.cli import _build_parser, _collect
from roller.harnesses.config_overrides import (
    config_path,
    parse_config_assignment,
    recorded_config_assignments,
    validate_config_assignments,
)
from roller.models import HarnessResult
from roller.wire_trace import REDACTED_VALUE


class ConfigAssignmentTests(unittest.TestCase):
    def test_parses_key_value_at_the_first_equals_sign(self) -> None:
        self.assertEqual(
            parse_config_assignment("a.b=x=y"), ("a.b", "x=y")
        )
        self.assertEqual(parse_config_assignment(" a.b =  "), ("a.b", "  "))
        for text in ("a.b", "=1", "-a=1", "a b=1", ".=1"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_config_assignment(text)

    def test_rejects_keys_openclaw_would_unescape_or_trim(self) -> None:
        # OpenClaw reads each key as gateway.port.
        for key in (r"g\ateway.port", r'gateway["\u0070ort"]', "gateway.port\ufeff"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "invalid configuration key"):
                    parse_config_assignment(f"{key}=1")
                with self.assertRaisesRegex(ValueError, "invalid configuration key"):
                    validate_config_assignments(((key, "1"),), {}, flag="--x")

    def test_dotted_and_bracketed_paths_name_the_same_segments(self) -> None:
        expected = ("a", "b.c", "d")
        for key in ('a["b.c"].d', "a['b.c'].d", "a[b.c][d]"):
            with self.subTest(key=key):
                self.assertEqual(config_path(key), expected)

    def test_rejects_paths_overlapping_managed_ones_in_either_direction(self) -> None:
        managed = {"gateway.auth.token": "managed"}
        for key in ("gateway", "gateway.auth", "gateway.auth.token", "gateway.auth.token.x"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "overlaps 'gateway.auth.token'"):
                    validate_config_assignments(((key, "1"),), managed, flag="--x")
        allowed = validate_config_assignments(
            (("gateway.http.port", "1"), ("gateway.authx", "2")), managed, flag="--x"
        )
        self.assertEqual(len(allowed), 2)

    def test_rejects_keys_overlapping_each_other_in_either_order(self) -> None:
        for keys in (("a.b", "a.b.c"), ("a.b.c", "a.b"), ("a.b", "a.b")):
            with self.subTest(keys=keys):
                with self.assertRaisesRegex(
                    ValueError, f"cannot set '{keys[1]}': it overlaps '{keys[0]}'"
                ):
                    validate_config_assignments(
                        [(key, "1") for key in keys], {}, flag="--x"
                    )
        allowed = validate_config_assignments((("a.b", "1"), ("a.c", "2")), {}, flag="--x")
        self.assertEqual(len(allowed), 2)

    def test_records_values_with_credential_leaves_redacted(self) -> None:
        self.assertEqual(
            recorded_config_assignments(
                (("channels.discord.token", "secret"), ("a.apiKey", "k"), ("a.b", "1"))
            ),
            {
                "channels.discord.token": REDACTED_VALUE,
                "a.apiKey": REDACTED_VALUE,
                "a.b": "1",
            },
        )


class _StubHarness:
    name = "openclaw"

    def __init__(self, config, slot) -> None:
        self.config = config

    def run(self, request, output_dir, attempt):
        return HarnessResult.error_result("not run in this test")


class CliConfigSetTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tasks = self.root / "tasks.jsonl"
        self.tasks.write_text(json.dumps({"id": "task", "prompt": "hi"}) + "\n")
        self.configs = []
        patches = contextlib.ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(
            patch("roller.cli._docker_preflight", return_value="test-image-id")
        )
        for constructor in ("OpenClawHarness", "CodexHarness"):
            patches.enter_context(
                patch(
                    f"roller.cli.{constructor}",
                    side_effect=lambda config, slot: self._harness(config, slot),
                )
            )

    def _harness(self, config, slot):
        self.configs.append(config)
        return _StubHarness(config, slot)

    def invoke(self, *extra: str, harness: str = "openclaw", resume: bool = False) -> int:
        args = [
            "collect", "--tasks", str(self.tasks), "--harness", harness,
            "--model", "provider/model", "--output", str(self.root / "runs"),
            "--run-name", "run", *extra, *(["--resume"] if resume else []),
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            return _collect(_build_parser().parse_args(args))

    def lock(self) -> dict:
        return json.loads((self.root / "runs" / "run" / "lock.json").read_text())

    def test_parser_collects_repeated_settings_and_rejects_malformed_ones(self) -> None:
        args = _build_parser().parse_args(
            [
                "collect", "--tasks", "t.jsonl",
                "--openclaw-config-set", "a.b=1",
                "--openclaw-config-set", "c=x=y",
                "--codex-config-set", "model_verbosity=\"low\"",
            ]
        )
        self.assertEqual(args.openclaw_config_set, [("a.b", "1"), ("c", "x=y")])
        self.assertEqual(args.codex_config_set, [("model_verbosity", '"low"')])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            _build_parser().parse_args(
                ["collect", "--tasks", "t.jsonl", "--openclaw-config-set", "a.b"]
            )

    def test_settings_reach_the_harness_and_are_locked_without_secrets(self) -> None:
        self.invoke(
            "--openclaw-config-set", "diagnostics.stuckSessionAbortMs=2700000",
            "--openclaw-config-set", "channels.discord.token=secret",
        )

        self.assertEqual(
            self.configs[0].config_set,
            (
                ("diagnostics.stuckSessionAbortMs", "2700000"),
                ("channels.discord.token", "secret"),
            ),
        )
        recorded = {
            "diagnostics.stuckSessionAbortMs": "2700000",
            "channels.discord.token": REDACTED_VALUE,
        }
        self.assertEqual(self.lock()["execution"]["openclaw"]["config_set"], recorded)
        info = json.loads((self.root / "runs" / "run" / "info.json").read_text())
        self.assertEqual(info["configuration"]["openclaw"]["config_set"], recorded)
        self.assertNotIn("secret", (self.root / "runs" / "run" / "lock.json").read_text())

    def test_unused_option_leaves_the_lock_unchanged_for_older_runs(self) -> None:
        self.invoke(harness="openclaw")
        self.assertNotIn("config_set", self.lock()["execution"]["openclaw"])

    def test_resume_requires_the_same_settings(self) -> None:
        self.invoke("--codex-config-set", "model_verbosity=\"low\"", harness="codex")
        self.assertEqual(
            self.lock()["execution"]["codex"]["config_set"],
            {"model_verbosity": '"low"'},
        )
        self.invoke("--codex-config-set", "model_verbosity=\"low\"", harness="codex", resume=True)
        with self.assertRaisesRegex(FileExistsError, "lock mismatch"):
            self.invoke(
                "--codex-config-set", "model_verbosity=\"high\"", harness="codex", resume=True
            )

    def test_setting_for_an_unselected_harness_is_an_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "--codex-config-set is only valid"):
            self.invoke("--codex-config-set", "model_verbosity=1", harness="openclaw")
        with self.assertRaisesRegex(ValueError, "--openclaw-config-set is only valid"):
            self.invoke("--openclaw-config-set", "a.b=1", harness="codex")

    def test_managed_path_is_rejected_before_any_run_files_are_written(self) -> None:
        with self.assertRaisesRegex(ValueError, "--gateway-port"):
            self.invoke("--openclaw-config-set", "gateway.port=1")
        self.assertFalse((self.root / "runs" / "run").exists())


if __name__ == "__main__":
    unittest.main()
