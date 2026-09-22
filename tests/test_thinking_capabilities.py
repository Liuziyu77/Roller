from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from roller.thinking_capabilities import load_thinking_capabilities


class ThinkingCapabilitiesTests(unittest.TestCase):
    def _load(self, payload: object):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "capabilities.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return load_thinking_capabilities(path)

    def test_exact_endpoint_profile_overrides_generic_profile(self) -> None:
        capabilities = self._load(
            {
                "schema_version": "roller.thinking-capabilities/v1",
                "profiles": [
                    {
                        "harness": "hermes",
                        "model": "served-model",
                        "supported_efforts": ["off", "low", "high"],
                    },
                    {
                        "harness": "hermes",
                        "model": "served-model",
                        "api_base": "http://gateway.test/v1/",
                        "supported_efforts": ["low", "high"],
                    },
                ],
            }
        )

        capabilities.validate(
            harness="hermes",
            model="served-model",
            api_base="http://other.test/v1",
            effort="off",
        )
        with self.assertRaisesRegex(ValueError, "not declared supported"):
            capabilities.validate(
                harness="hermes",
                model="served-model",
                api_base="http://gateway.test/v1",
                effort="off",
            )

    def test_packaged_example_is_valid(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "examples"
            / "thinking-capabilities.json"
        )
        capabilities = load_thinking_capabilities(path)
        profile = capabilities.match(
            harness="hermes",
            model="served-model-id",
            api_base="https://gateway.example/v1",
        )
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile.supported_efforts, {"low", "medium", "high"})

    def test_unmatched_model_remains_provider_validated(self) -> None:
        capabilities = self._load(
            {
                "schema_version": "roller.thinking-capabilities/v1",
                "profiles": [],
            }
        )
        self.assertIsNone(
            capabilities.validate(
                harness="pi",
                model="unknown-model",
                api_base="http://gateway.test/v1",
                effort="max",
            )
        )

    def test_rejects_invalid_levels_and_duplicate_profiles(self) -> None:
        for profiles, message in (
            (
                [
                    {
                        "harness": "pi",
                        "model": "model",
                        "supported_efforts": ["turbo"],
                    }
                ],
                "Roller thinking levels",
            ),
            (
                [
                    {
                        "harness": "pi",
                        "model": "model",
                        "supported_efforts": ["off"],
                    },
                    {
                        "harness": "pi",
                        "model": "model",
                        "supported_efforts": ["low"],
                    },
                ],
                "duplicate",
            ),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                ValueError, message
            ):
                self._load(
                    {
                        "schema_version": "roller.thinking-capabilities/v1",
                        "profiles": profiles,
                    }
                )


if __name__ == "__main__":
    unittest.main()
