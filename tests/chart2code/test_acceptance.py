import copy
import json
from pathlib import Path
import tempfile
import unittest

from roller_chart2code.acceptance import audit_sample, write_report
from chart2code.acceptance_fixture import dump, fixture, image, refresh, step
from chart2code.png_fixture import png


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        self.sample = self.run / "samples/tiny"
        self.contract, self.actf, self.info = fixture(self.sample)

    def audit(self, **kwargs):
        return audit_sample(self.sample, self.contract, **kwargs)

    def test_single_render_and_report_only(self):
        report = self.audit()
        self.assertTrue(report["qualified"], report)
        self.assertFalse(self.audit(report_only=True)["qualified"])
        self.assertEqual(report["visual_quality"], "not_evaluated")
        self.assertIn("audited_at", report)
        self.assertIn("implementation", report)

    def test_reference_missing_wrong_or_only_auxiliary(self):
        self.actf["steps"][0]["user_content"].pop()
        self.actf["visual_evidence"] = {"reference_sha256": self.contract["reference_sha256"]}
        refresh(self.sample, self.actf)
        self.assertFalse(self.audit()["qualified"])
        self.actf["steps"][0]["user_content"].append(image(png()))
        refresh(self.sample, self.actf)
        self.assertFalse(self.audit()["qualified"])

    def test_reference_after_render_is_not_initial_delivery(self):
        reference = self.actf["steps"][0]["user_content"].pop()
        self.actf["steps"].insert(1, step(2, call="late", content=[reference]))
        self.actf["steps"][-1]["step_id"] = 3
        refresh(self.sample, self.actf)
        self.assertFalse(self.audit()["qualified"])

    def test_execution_and_generic_training_do_not_get_overwritten(self):
        for key, value in (("status", "error"), ("training", {"eligible": False}),
                           ("conversion", {"status": "unavailable"})):
            with self.subTest(key=key):
                info = {**self.info, key: value}
                refresh(self.sample, info=info)
                before = (self.sample / "info.json").read_bytes()
                self.assertFalse(self.audit()["qualified"])
                self.assertEqual((self.sample / "info.json").read_bytes(), before)

    def test_tampered_missing_and_unlisted_artifact(self):
        path = self.sample / "results/iterations/attempt_000001/stdout.log"
        path.write_text("tampered")
        self.assertFalse(self.audit()["qualified"])
        path.write_text("")
        self.assertTrue(self.audit()["qualified"])
        (self.sample / "results/unlisted.txt").write_text("extra")
        self.assertFalse(self.audit()["qualified"])

    def test_bad_shapes_reject_without_uncaught_attribute_error(self):
        for key in ("runtime", "usage", "training", "output_collection"):
            with self.subTest(key=key):
                dump(self.sample / "info.json", {**self.info, key: ["bad"]})
                self.assertFalse(self.audit()["qualified"])

    def test_single_render_output_view_is_forbidden(self):
        self.actf["steps"].insert(1, step(2, call="view", content=[image(png())]))
        self.actf["steps"][-1]["step_id"] = 3
        refresh(self.sample, self.actf)
        self.assertFalse(self.audit()["qualified"])

    def test_fingerprint_stale_report_cannot_grant_qualification(self):
        report = self.audit()
        path = write_report(self.run, "samples/tiny", report)
        self.assertTrue(path.is_relative_to(self.run / "chart2code/acceptance"))
        self.info["usage"]["request_count"] = 2
        refresh(self.sample, info=self.info)
        changed = self.audit()
        self.assertNotEqual(changed["fingerprint"], report["fingerprint"])
        self.assertTrue(changed["qualified"])

    def test_no_journal_and_diagnostic_actf_reject(self):
        manifest = self.sample / "results/iterations/render_manifest.jsonl"
        manifest.unlink()
        self.assertFalse(self.audit()["qualified"])
        actf = copy.deepcopy(self.actf)
        actf["projection"] = {"complete": False}
        dump(self.sample / "trace_ACTF.json", actf)
        self.assertFalse(self.audit()["qualified"])

    def test_contract_prompt_and_identity_cannot_be_relaxed(self):
        self.contract["profile"]["prompt"] = "shortened"
        with self.assertRaisesRegex(ValueError, "frozen profile"):
            self.audit()

    def test_unsafe_inventory_fails_closed(self):
        (self.sample / "results/escape").symlink_to(self.run)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.audit()

    def test_truncated_raw_tail_rejects(self):
        with (self.sample / "trace.jsonl").open("a") as stream:
            stream.write('{"truncated":')
        self.assertFalse(self.audit()["qualified"])

    def test_full_wire_requirement_comes_from_contract_not_optional_runtime_metadata(self):
        self.contract["capture"] = {"require_wire_trace": True}
        self.assertFalse(self.audit()["qualified"])
        (self.sample / "wire_trace.jsonl").write_text('{"request":{}}\n')
        self.assertTrue(self.audit()["qualified"])

    def test_render_budget_uses_all_attempts(self):
        import subprocess
        from chart2code.acceptance_fixture import ROOT
        outputs = self.sample / "results"
        outputs.rename(self.sample / "outputs")
        try:
            result = subprocess.run(["bash", str(ROOT / "docker/chart2code/python-with-snapshots.sh"), "outputs/plot.py"],
                                    cwd=self.sample, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0)
        finally:
            (self.sample / "outputs").rename(outputs)
        refresh(self.sample)
        report = self.audit()
        self.assertFalse(report["qualified"])
        self.assertIn("budget", [c["id"] for c in report["gates"]["artifact"]["checks"] if not c["passed"]])

    def test_loop_view_requires_proven_order_and_remains_pending_q6(self):
        sample = self.run / "samples/loop"
        contract, actf, info = fixture(sample, "loop-v3", markers=True, output_view=True)
        report = audit_sample(sample, contract)
        self.assertTrue(report["three_gates_passed"], report)
        self.assertFalse(report["qualified"])
        self.assertEqual(report["profile_verdict"], "pending_Q6")
        # Same image before a render does not establish post-render inspection.
        actf["steps"][0]["observation"][0]["content"] = [image(png())]
        refresh(sample, actf, info)
        report = audit_sample(sample, contract)
        self.assertFalse(report["three_gates_passed"], report)


if __name__ == "__main__":
    unittest.main()
