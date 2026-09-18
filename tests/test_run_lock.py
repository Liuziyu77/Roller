from __future__ import annotations

import json
from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

from roller.models import RunRequest, Task, TaskBundle
from roller.harness_registry import HARNESS_NAMES
from roller.run_lock import (
    LEGACY_MIGRATION_FILENAME,
    build_run_lock,
    complete_legacy_adoption,
    ensure_run_lock,
)


def _request(root: Path, digest: str = "bundle-a") -> RunRequest:
    manifest = root / "task.json"
    manifest.write_text("{}", encoding="utf-8")
    bundle = TaskBundle(
        root=root,
        inputs_root=root,
        context_root=root,
        manifest_path=manifest,
        input_manifest_path=manifest,
        context_manifest_path=manifest,
        manifest_digest=digest,
        input_tree_digest="input-tree",
        input_manifest_digest="input-manifest",
        context_manifest_digest="context-manifest",
    )
    return RunRequest(
        0,
        Task(id="task", prompts=("hello",), source_index=0),
        "proxy/model",
        30,
        bundle,
    )


class RunLockTests(unittest.TestCase):
    def test_disabled_retired_options_are_compared_without_rewriting_old_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lock.json"
            expected = {"execution": {name: {"thinking": "high"} for name in HARNESS_NAMES}}
            recorded = deepcopy(expected)
            for settings in recorded["execution"].values():
                settings["external_thinking"] = "off"
            recorded["execution"]["claude-code"].update(
                signature_recovery="off",
                signature_recovery_attempts=3,
                signature_recovery_max_tokens=12000,
            )
            path.write_text(json.dumps(recorded))
            before = path.read_bytes(), path.stat().st_mtime_ns
            expected_before = deepcopy(expected)

            decision = ensure_run_lock(path, expected, resume=True, existing_run=True)

            self.assertTrue(decision.reuse_existing_samples)
            self.assertFalse(decision.created)
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
            self.assertEqual(expected, expected_before)

    def test_retired_compatibility_does_not_hide_active_unknown_or_other_changes(self) -> None:
        cases = [
            ("codex", {"external_thinking": mode})
            for mode in ("external", "external_always", "unknown", None, False)
        ] + [
            ("claude-code", {"signature_recovery": mode})
            for mode in ("fill", "prefer", "unknown", None, False)
        ] + [
            ("claude-code", {"signature_recovery_attempts": 1}),
            ("claude-code", {"signature_recovery_max_tokens": 8192}),
            ("codex", {"external_thinking": "off", "thinking": "low"}),
            ("unknown-harness", {"external_thinking": "off"}),
        ]
        for name, changes in cases:
            with self.subTest(name=name, changes=changes), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "lock.json"
                expected = {"execution": {name: {"thinking": "high"}}}
                recorded = deepcopy(expected)
                recorded["execution"][name].update(changes)
                path.write_text(json.dumps(recorded))
                before = path.read_bytes()
                with self.assertRaisesRegex(FileExistsError, "lock mismatch"):
                    ensure_run_lock(path, expected, resume=True, existing_run=True)
                self.assertEqual(path.read_bytes(), before)

    def test_builds_secret_safe_structural_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock = build_run_lock(
                [_request(Path(temporary))],
                roller_version="1.2.3",
                execution={"thinking": "high"},
                model_config={
                    "providers": {
                        "proxy": {
                            "baseUrl": (
                                "https://user:password@example.test/v1"
                                "?api-version=2026-08-01&token=secret#fragment"
                            ),
                            "apiKey": "secret-key",
                        }
                    }
                },
            )

        self.assertEqual(lock["requests"][0]["task_bundle"]["manifest_digest"], "bundle-a")
        provider = lock["model_config"]["providers"]["proxy"]
        self.assertEqual(
            provider["baseUrl"],
            "https://example.test/v1?api-version=2026-08-01&token=%3Credacted%3E",
        )
        self.assertEqual(provider["apiKey"], "<redacted>")
        self.assertNotIn("secret-key", json.dumps(lock))
        self.assertNotIn("password", json.dumps(lock))
        self.assertNotIn("token=secret", json.dumps(lock))

    def test_reuses_only_an_equal_existing_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "lock.json"
            lock = build_run_lock(
                [_request(root)],
                roller_version="1.2.3",
                execution={"thinking": "high"},
                model_config=None,
            )
            created = ensure_run_lock(
                path, lock, resume=False, existing_run=False
            )
            reused = ensure_run_lock(path, lock, resume=True, existing_run=True)

            self.assertTrue(created.created)
            self.assertFalse(created.reuse_existing_samples)
            self.assertFalse(reused.created)
            self.assertTrue(reused.reuse_existing_samples)

            changed = {**lock, "execution": {"thinking": "low"}}
            with self.assertRaisesRegex(FileExistsError, r"execution.thinking"):
                ensure_run_lock(path, changed, resume=True, existing_run=True)

    def test_adopts_legacy_run_without_reusing_unlocked_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "info.json").write_text("{}", encoding="utf-8")
            lock = build_run_lock(
                [_request(root)],
                roller_version="1.2.3",
                execution={},
                model_config=None,
            )

            decision = ensure_run_lock(
                root / "lock.json", lock, resume=True, existing_run=True
            )

            self.assertTrue(decision.legacy_run)
            self.assertFalse(decision.reuse_existing_samples)
            self.assertTrue((root / "lock.json").is_file())
            self.assertTrue((root / LEGACY_MIGRATION_FILENAME).is_file())

            interrupted = ensure_run_lock(
                root / "lock.json", lock, resume=True, existing_run=True
            )
            self.assertTrue(interrupted.legacy_run)
            self.assertFalse(interrupted.reuse_existing_samples)

            complete_legacy_adoption(root / "lock.json")
            trusted = ensure_run_lock(
                root / "lock.json", lock, resume=True, existing_run=True
            )
            self.assertFalse(trusted.legacy_run)
            self.assertTrue(trusted.reuse_existing_samples)

    def test_refuses_to_overwrite_invalid_existing_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lock.json"
            path.write_text("not json", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                ensure_run_lock(
                    path,
                    {"schema_version": "roller.run-lock/v1"},
                    resume=True,
                    existing_run=True,
                )


if __name__ == "__main__":
    unittest.main()
