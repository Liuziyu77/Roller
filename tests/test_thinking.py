from __future__ import annotations

import unittest

from roller.harnesses.openclaw import OpenClawConfig
from roller.models import Task, UserSimulatorSpec
from roller.thinking import THINKING_LEVELS, normalize_thinking, resolve_thinking


class ThinkingTests(unittest.TestCase):
    def test_accepts_every_openclaw_logical_level(self) -> None:
        for level in THINKING_LEVELS:
            with self.subTest(level=level):
                self.assertEqual(normalize_thinking(level), level)
                self.assertEqual(
                    OpenClawConfig(docker_image="test-image", thinking=level).thinking,
                    level,
                )

    def test_normalizes_gpt_none_alias_to_openclaw_off(self) -> None:
        self.assertEqual(normalize_thinking("none"), "off")
        self.assertEqual(normalize_thinking(" NONE "), "off")
        self.assertEqual(Task("task", (), 0, thinking="none").thinking, "off")
        self.assertEqual(
            UserSimulatorSpec("simulate", thinking="none").thinking,
            "off",
        )
        self.assertEqual(
            OpenClawConfig(docker_image="test-image", thinking="none").thinking,
            "off",
        )

    def test_task_override_is_validated_and_does_not_use_truthiness_fallback(self) -> None:
        self.assertEqual(resolve_thinking("max", "low"), "max")
        with self.assertRaisesRegex(ValueError, "task thinking must be one of"):
            Task("task", (), 0, thinking="turbo")
        with self.assertRaisesRegex(ValueError, "task thinking must be one of"):
            Task("task", (), 0, thinking="")

    def test_rejects_non_string_and_unknown_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "thinking must be a string"):
            normalize_thinking(1)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "thinking must be one of"):
            normalize_thinking("turbo")


if __name__ == "__main__":
    unittest.main()
