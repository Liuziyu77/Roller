from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from roller.cli import _build_parser
from roller.collector import CollectionConfig, Collector
from roller.harnesses.openclaw import OpenClawConfig
from roller.models import HarnessResult, RunRequest, Task


class RuntimeRegressionTests(unittest.TestCase):
    def test_brave_is_explicit_not_implicitly_inherited(self):
        self.assertNotIn("BRAVE_API_KEY", OpenClawConfig().pass_env)
        self.assertIn("BRAVE_API_KEY", OpenClawConfig(pass_env=("BRAVE_API_KEY",)).pass_env)

    def test_attempt_backoff_preserves_original_per_attempt_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Mock(name="fake")
            harness.run.return_value = HarnessResult.error_result("failed")
            collector = Collector(CollectionConfig(Path(tmp), retries=2, retry_backoff_seconds=3),
                                  harness_factory=lambda _: harness)
            request = RunRequest(0, Task(id="sample", prompts=("hello",), source_index=0), "model", 10)
            with patch.object(collector, "_finalize_sample", return_value={}) as finalize, \
                    patch("roller.collector.time.sleep") as sleep:
                collector._run_one(request, 0)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [3, 6])
            self.assertEqual(harness.run.call_count, 3)
            self.assertTrue(all(call.args[0].timeout_seconds == 10 for call in harness.run.call_args_list))
            attempts = finalize.call_args.args[4]
            self.assertEqual([row.get("retry_backoff_seconds") for row in attempts], [3, 6, None])

    def test_default_zero_retries_does_not_sleep(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Mock()
            harness.run.return_value = HarnessResult.error_result("failed")
            collector = Collector(CollectionConfig(Path(tmp)), harness_factory=lambda _: harness)
            request = RunRequest(0, Task(id="sample", prompts=("hello",), source_index=0), "model", 10)
            with patch.object(collector, "_finalize_sample", return_value={}), \
                    patch("roller.collector.time.sleep") as sleep:
                collector._run_one(request, 0)
            sleep.assert_not_called()
            self.assertEqual(harness.run.call_count, 1)

    def test_invalid_backoff_is_rejected(self):
        for value in (-1, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CollectionConfig(Path("unused"), retry_backoff_seconds=value)

    def test_cli_exposes_attempt_backoff(self):
        args = _build_parser().parse_args(["collect", "--tasks", "tasks.jsonl", "--retry-backoff", "2.5"])
        self.assertEqual(args.retry_backoff, 2.5)
