from __future__ import annotations

import unittest

from roller.openclaw_usage import OpenClawUsage


class OpenClawUsageTests(unittest.TestCase):
    def test_absent_usage_differs_from_cost_only_usage(self) -> None:
        self.assertIsNone(OpenClawUsage.parse(None))
        self.assertIsNone(OpenClawUsage.parse("invalid"))
        usage = OpenClawUsage.parse({"cost": {"total": 0.05}})
        self.assertEqual(usage.counts, (0, 0, 0, 0, 0))
        self.assertEqual(usage.cost_usd, 0.05)
        self.assertTrue(usage.matchable)

    def test_total_fallback_includes_cache_and_keeps_explicit_zero(self) -> None:
        for total, expected in ((None, 16), (0, 0), (20, 20)):
            with self.subTest(total=total):
                usage = OpenClawUsage.parse({
                    "input": 10, "output": 2, "cacheRead": 3,
                    "cacheWrite": 1, "totalTokens": total,
                })
                self.assertEqual(usage.counts, (10, 2, 3, 1, expected))
                self.assertTrue(usage.matchable)
        self.assertEqual(OpenClawUsage.parse({"input": 10}).counts[-1], 10)

    def test_invalid_values_are_sanitized_but_not_matchable(self) -> None:
        for invalid in (True, -1, "10", float("nan"), float("inf")):
            for field in ("input", "totalTokens"):
                with self.subTest(invalid=invalid, field=field):
                    usage = OpenClawUsage.parse({
                        field: invalid, "output": 2, "cost": {"total": invalid},
                    })
                    self.assertEqual(usage.counts, (0, 2, 0, 0, 2))
                    self.assertEqual(usage.cost_usd, 0.0)
                    self.assertFalse(usage.matchable)


if __name__ == "__main__":
    unittest.main()
