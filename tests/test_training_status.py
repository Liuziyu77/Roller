import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from roller.collector import CollectionConfig, Collector
from roller.conversation import ConversationManager
from roller.integrity import validate_resumable_sample
from roller.models import HarnessResult
from roller.trajectory_status import conversion_outcome, export_sft
from test_collector import FakeHarness, make_requests


class TrainingStatusTests(unittest.TestCase):
    def state(self, **kwargs):
        return {"lock": threading.Lock(), "calls": {}, "active": 0, "max_active": 0, **kwargs}

    def test_raw_only_all_harnesses_resume_without_reexecution(self):
        for name in ("codex", "claude-code", "pi", "deepseek", "opencode", "openclaw"):
            with self.subTest(harness=name), tempfile.TemporaryDirectory() as tmp:
                state = self.state(raw_only=True, harness_name=name)
                config = CollectionConfig(run_dir=Path(tmp), retries=2, resume=True)
                collector = Collector(config, lambda slot: FakeHarness(state, slot))
                request = make_requests()[:1]
                first = collector.collect(request)[0]
                self.assertEqual(first["status"], "ok")
                self.assertFalse(first["training"]["eligible"])
                self.assertEqual(first["conversion"]["status"], "unavailable")
                self.assertEqual(len(first["attempts"]), 1)
                second = collector.collect(request)[0]
                self.assertEqual(second["status"], "skipped")
                self.assertEqual(second["resumed_from_status"], "ok")
                self.assertFalse(second["training"]["eligible"])
                self.assertEqual(sum(state["calls"].values()), 1)
                self.assertEqual((Path(tmp) / "sft.jsonl").read_text(), "")

    def test_undocumented_missing_actf_is_failed_not_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self.state(raw_only=True, structured_conversion_error=False)
            collector = Collector(CollectionConfig(run_dir=Path(tmp)), lambda slot: FakeHarness(state, slot))
            request = make_requests()[0]
            result = collector.collect([request])[0]
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["attempts"][0]["status"], "ok")
            with self.assertRaisesRegex(ValueError, "conversion failed"):
                validate_resumable_sample(collector._sample_dir(request), request, result)

    def test_incomplete_projection_never_enters_sft_even_if_execution_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            state = self.state()
            request = make_requests()[0]
            final = FakeHarness(state, 0).run(request, base, 1)
            path = Path(final.trajectory_path)
            actf = json.loads(path.read_text())
            actf["projection"] = {"complete": False, "training_eligible": False}
            path.write_text(json.dumps(actf))
            collector = Collector(CollectionConfig(run_dir=base), lambda slot: FakeHarness(state, slot))
            result = collector._finalize_sample(request, base, "fake", final, [{"status": "ok"}], "now")
            self.assertEqual(result["status"], "ok")
            self.assertFalse(result["training"]["eligible"])
            self.assertIsNone(result["trajectory"]["sft"])

    def test_stale_sft_is_removed_after_failed_conversion(self):
        for content in ("not JSON", "[]", '{"schema_version":"ACTF_v1.0","steps":[]}'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                (base / "trace.jsonl").write_text('{}\n')
                (base / "trace_ACTF.json").write_text(content)
                (base / "trace_SFT.json").write_text('{"stale":true}')
                metadata = {}
                export_sft(base, metadata)
                self.assertEqual(conversion_outcome(base, metadata)["status"], "failed")
                self.assertFalse((base / "trace_SFT.json").exists())

    def test_aggregation_revalidates_actf_not_just_sft_file_existence(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self.state()
            collector = Collector(CollectionConfig(run_dir=Path(tmp)), lambda slot: FakeHarness(state, slot))
            request = make_requests()[0]
            result = collector.collect([request])[0]
            path = collector._sample_dir(request) / "trace_ACTF.json"
            actf = json.loads(path.read_text())
            actf["projection"] = {"complete": False}
            path.write_text(json.dumps(actf))
            with self.assertRaisesRegex(RuntimeError, "incomplete diagnostic"):
                collector._write_sft_dataset([result])

    def test_simulator_conversion_is_not_an_execution_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "trace.jsonl").write_text('{}\n')
            (base / "trace_ACTF.json").write_text('not JSON')
            final = HarnessResult(status="ok", elapsed_seconds=0, usage={},
                                  trace_path="trace.jsonl", trajectory_path="trace_ACTF.json", metadata={})
            info = ConversationManager._archive_simulator(base, final, "model", "pi", keep_aux_traces=False)
            self.assertEqual(info["status"], "ok")
            self.assertEqual(info["conversion"]["status"], "failed")
            self.assertFalse(info["training"]["eligible"])

    def test_cli_session_finalizers_keep_conversion_separate(self):
        from roller.harnesses.codex import CodexConfig, CodexHarness, CodexSession
        from roller.harnesses.claude_code import ClaudeCodeConfig, ClaudeCodeHarness, ClaudeCodeSession
        for name, config_type, harness_type, session_type in (
            ("codex", CodexConfig, CodexHarness, CodexSession),
            ("claude_code", ClaudeCodeConfig, ClaudeCodeHarness, ClaudeCodeSession),
        ):
            for missing in (False, True):
                with self.subTest(harness=name, missing=missing), tempfile.TemporaryDirectory() as tmp:
                    base = Path(tmp)
                    trace = base / "trace.jsonl"
                    trace.write_text('{}\n')
                    harness = harness_type(config_type(wire_trace_mode="off"), 0, Mock())
                    harness._image_digest = Mock(return_value="sha256:test")
                    harness._collect_trace = Mock(return_value=None if missing else trace)
                    harness._stop_wire_proxy = Mock()
                    session = session_type(harness, make_requests()[0], base, 1, collect_outputs=False)
                    session.container_started = True
                    with patch(f"roller.harnesses.{name}.convert_{name}_trace", side_effect=ValueError("cannot project")):
                        result = session.finish("ok")
                    self.assertEqual(result.status, "ok")
                    self.assertIsNone(result.error)
                    self.assertEqual(result.metadata["trajectory_conversion"]["target"], "raw" if missing else "actf")
