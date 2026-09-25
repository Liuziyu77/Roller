from copy import deepcopy
import hashlib
import unittest

from roller_chart2code.profiles import freeze_profile


class ProfileTests(unittest.TestCase):
    def test_single_render_does_not_require_or_allow_output_view(self):
        profile = freeze_profile("single-render-v2", "inputs/reference.png")
        self.assertEqual(profile["max_render_attempts"], 1)
        self.assertEqual(profile["output_view_policy"], "forbidden")
        self.assertEqual(profile["revision_policy"], "forbidden")
        self.assertIn("Do not open the generated image", profile["prompt"])
        self.assertEqual(profile["prompt_sha256"], hashlib.sha256(profile["prompt"].encode()).hexdigest())

    def test_historical_loop_allows_optional_revision(self):
        for name in ("loop-v1", "loop-v2", "loop-v3", "loop-v4-hard", "loop-v5-evidence"):
            with self.subTest(profile=name):
                profile = freeze_profile(name, "inputs/reference.png")
                self.assertTrue(profile["qualification_ready"])
                self.assertEqual(profile["revision_policy"], "optional")
                self.assertEqual(profile["scope"], "historical_experiment")
                self.assertEqual(profile["unresolved_questions"], [])
                self.assertEqual(profile["output_view_policy"], "every_successful_render")

    def test_budget_and_reference_are_part_of_profile_fingerprint(self):
        before = freeze_profile("loop-v5-evidence", "inputs/a.png")
        after = freeze_profile("loop-v5-evidence", "inputs/a.png", max_renders=8)
        moved = freeze_profile("loop-v5-evidence", "inputs/b.png")
        self.assertEqual(before["prompt_template_sha256"], after["prompt_template_sha256"])
        self.assertEqual(len({p["profile_sha256"] for p in (before, after, moved)}), 3)
        self.assertNotIn("{max_renders}", after["prompt"])

    def test_no_silent_budget_relaxation_or_unsafe_paths(self):
        for name, reference, maximum in (("unknown", "a.png", None), ("loop-v1", "a.png", 8),
                                         ("loop-v4-hard", "a.png", 21), ("single-render-v2", "a\nb.png", None),
                                         ("single-render-v2", "a.png", True)):
            with self.subTest(profile=name, maximum=maximum), self.assertRaises(ValueError):
                freeze_profile(name, reference, max_renders=maximum)
