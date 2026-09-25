import base64
from contextlib import redirect_stderr
from dataclasses import replace
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from roller.bundles import TaskBundleStore
from roller.collector import CollectionConfig, Collector
from roller.models import HarnessResult, InputFile, RunRequest, Task
from roller_chart2code.acceptance import audit_sample, evidence_inventory, json_object
from roller_chart2code.export import audit_run, export_run, main
from roller_chart2code.pipeline import Chart2CodeCollector, EvidenceChanged, ResumeRejected, contract_for
from chart2code.acceptance_fixture import dump, fixture


class SyntheticHarness:
    name = "fake"

    def __init__(self, calls):
        self.calls = calls

    def run(self, request, output_dir, attempt):
        self.calls.append(request.index)
        _, _, info = fixture(output_dir)
        return HarnessResult(status="ok", elapsed_seconds=0, usage={"request_count": 1},
            trace_path="trace.jsonl", trajectory_path="trace_ACTF.json",
            metadata={"output_collection": info["output_collection"]})


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / "run"
        contract, actf, _ = fixture(self.root / "seed")
        reference = self.root / "reference.png"
        reference.write_bytes(base64.b64decode(actf["steps"][0]["user_content"][1]["data"]))
        task = Task("tiny", (contract["profile"]["prompt"],), 0, harness="fake",
                    inputs=(InputFile(reference, "inputs/reference.png"),), attach_images=("inputs/reference.png",))
        store = TaskBundleStore(self.root / "bundles")
        self.request = RunRequest(0, task, "synthetic", 30, bundle=store.prepare(task))
        self.calls = []

    def collector(self, requests=None, **kwargs):
        requests = requests or [self.request]
        return Chart2CodeCollector(CollectionConfig(self.run, **kwargs), lambda slot: SyntheticHarness(self.calls),
            contracts={r.index: contract_for(r, "single-render-v2", "inputs/reference.png") for r in requests},
            execution={"harness": "fake", "model": "synthetic", "endpoint": "none"})

    def test_collect_resume_and_offline_export(self):
        first = self.collector()
        result = first.collect([self.request])[0]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(first.task_summary["qualified"], 1)
        sample = first._sample_dir(self.request)
        original = evidence_inventory(sample)
        # Missing/stale verifier report does not require paid rerun.
        for path in (self.run / "chart2code/acceptance").glob("*.json"):
            dump(path, {"schema_version": "old", "qualified": False})
        second = self.collector(resume=True)
        result = second.collect([self.request])[0]
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["resumed_from_status"], "ok")
        self.assertEqual(self.calls, [0])
        self.assertEqual(evidence_inventory(sample), original)
        manifest = export_run(self.run, self.root / "dataset")
        self.assertEqual(manifest["count"], 1)
        self.assertEqual(manifest["selected"][0]["source_run_status"], "skipped")
        with (self.root / "dataset/sft.jsonl").open() as stream:
            row = json.loads(next(stream))
            self.assertIn("images", row)
            self.assertEqual(list(stream), [])
        with self.assertRaises(FileExistsError):
            export_run(self.run, self.root / "dataset")

    def test_invalid_old_sample_stops_all_calls_before_metadata_or_pruning(self):
        first = self.collector()
        first.collect([self.request])
        sample = first._sample_dir(self.request)
        (sample / "results/output.png").write_bytes(b"tampered")
        before = evidence_inventory(sample)
        run_info = (self.run / "info.json").read_bytes()
        with self.assertRaises(ResumeRejected):
            self.collector(resume=True).collect([self.request])
        self.assertEqual(self.calls, [0])
        self.assertEqual(evidence_inventory(sample), before)
        self.assertEqual((self.run / "info.json").read_bytes(), run_info)
        with self.assertRaisesRegex(ValueError, "no qualified"):
            export_run(self.run, self.root / "invalid-dataset")
        self.assertFalse((self.root / "invalid-dataset").exists())

    def test_existing_unfinished_directory_never_auto_retries(self):
        collector = self.collector(resume=True)
        sample = collector._sample_dir(self.request)
        sample.mkdir(parents=True)
        (sample / "sentinel").write_text("preserve")
        with self.assertRaises(ResumeRejected):
            collector.collect([self.request])
        self.assertEqual(self.calls, [])
        self.assertEqual((sample / "sentinel").read_text(), "preserve")

    def test_config_and_prompt_drift_rejected(self):
        self.collector().collect([self.request])
        changed = self.collector(resume=True)
        changed.execution["model"] = "different"
        with self.assertRaisesRegex(ResumeRejected, "configuration"):
            changed.collect([self.request])
        bad = replace(self.request, task=replace(self.request.task, prompts=("shortened",)))
        with self.assertRaisesRegex(ValueError, "prompt"):
            self.collector([bad], resume=True)
        self.assertEqual(self.calls, [0])

    def test_concurrent_changed_sample_escapes_destructive_main_error_handler(self):
        collector = self.collector()
        collector.collect([self.request])
        sample = collector._sample_dir(self.request)
        resumed = self.collector(resume=True)
        original_collect = Collector.collect
        def change_then_collect(instance, requests):
            (sample / "sentinel").write_text("external change")
            (sample / "trace.jsonl").write_text('{"changed":true}\n')
            return original_collect(instance, requests)
        with patch.object(Collector, "collect", change_then_collect), self.assertRaises(EvidenceChanged):
            resumed.collect([self.request])
        self.assertEqual(self.calls, [0])
        self.assertTrue((sample / "sentinel").exists())
        self.assertEqual(json_object(sample / "info.json")["status"], "ok")

    def test_model_label_multi_sample_paths_and_report_only(self):
        requests = [replace(self.request, index=i, sample_index=i, sample_count=2, model_label="local label") for i in range(2)]
        collector = self.collector(requests, parallel=2)
        results = collector.collect(requests)
        self.assertEqual(collector.task_summary["qualified"], 2)
        self.assertTrue(collector._sample_dir(requests[1]).name.endswith("__sample_002"))
        self.assertIn("local_label", str(collector._sample_dir(requests[0])))
        self.assertEqual([r["model"] for r in results], ["synthetic", "synthetic"])
        self.assertEqual(audit_run(self.run, report_only=True)["qualified"], 0)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["export", "--run-dir", str(self.run), "--output", str(self.root / "bad"), "--report-only"])

    def test_export_reaudits_tampered_old_pass_report(self):
        collector = self.collector()
        collector.collect([self.request])
        sample = collector._sample_dir(self.request)
        dump(sample / "trace_SFT.json", {"messages": [], "tools": []})
        with self.assertRaisesRegex(ValueError, "no qualified"):
            export_run(self.run, self.root / "bad")
        self.assertEqual(audit_run(self.run)["qualified"], 0)

    def test_input_bytes_and_bundle_digest_checked_before_resume(self):
        self.collector().collect([self.request])
        changed = replace(self.request, bundle=replace(self.request.bundle, manifest_digest="changed"))
        with self.assertRaises(ResumeRejected):
            self.collector([changed], resume=True).collect([changed])
        self.assertEqual(self.calls, [0])

    def test_bad_existing_sample_is_checked_before_a_new_sample_is_launched(self):
        requests = [replace(self.request, index=i, sample_index=i, sample_count=2) for i in range(2)]
        first = self.collector(requests)
        first.collect(requests)
        # Simulate an interrupted run with a missing earlier sample, retaining its data elsewhere.
        first._sample_dir(requests[0]).rename(self.root / "preserved-first-sample")
        bad = first._sample_dir(requests[1])
        (bad / "results/output.png").write_bytes(b"tampered")
        self.calls.clear()
        with self.assertRaises(ResumeRejected):
            self.collector(requests, resume=True).collect(requests)
        self.assertEqual(self.calls, [])
        self.assertFalse(first._sample_dir(requests[0]).exists())

    def test_symlink_run_root_is_rejected_before_metadata_writes(self):
        self.run.symlink_to(self.root / "seed", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.collector()
        self.assertFalse((self.root / "seed/chart2code").exists())

    def test_explicit_sample_retry_keeps_upstream_collector_semantics(self):
        collector = self.collector(retries=1)
        calls = []
        class OnceFailingHarness(SyntheticHarness):
            def run(inner, request, output_dir, attempt):
                calls.append((attempt, request.timeout_seconds))
                if attempt == 1:
                    return HarnessResult.error_result("synthetic transient failure")
                return super().run(request, output_dir, attempt)
        collector.harness_factory = lambda slot: OnceFailingHarness(self.calls)
        result = collector.collect([self.request])[0]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, [(1, 30), (2, 30)])
        self.assertEqual(collector.task_summary["qualified"], 1)


if __name__ == "__main__":
    unittest.main()
