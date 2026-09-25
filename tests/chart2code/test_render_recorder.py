from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from roller_chart2code.render_journal import validate_render_journal
from chart2code.png_fixture import png as _png

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "docker/chart2code/python-with-snapshots.sh"


class RenderRecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.outputs = self.root / "outputs"
        self.outputs.mkdir()
        self.script = self.outputs / "plot.py"
        self.good = "from pathlib import Path\nPath(__file__).with_name('output.png').write_bytes(" + repr(_png()) + ")\n"
        self.script.write_text(self.good)

    def run_render(self):
        return subprocess.run(["bash", str(WRAPPER), "outputs/plot.py"], cwd=self.root,
                              capture_output=True, text=True, timeout=10)

    def rows(self):
        return [json.loads(line) for line in (self.outputs / "iterations/render_manifest.jsonl").read_text().splitlines()]

    def test_failure_does_not_restore_image_or_add_output(self):
        self.assertEqual(self.run_render().returncode, 0)
        self.script.write_text("from pathlib import Path\nimport sys\nPath('outputs/output.png').write_bytes(b'partial')\nprint('out')\nprint('err', file=sys.stderr)\nsys.exit(7)\n")
        result = self.run_render()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, "out\n", "err\n"))
        self.assertEqual((self.outputs/'output.png').read_bytes(), b'partial')
        self.assertEqual((self.outputs/'iterations/attempt_000002/candidate.png').read_bytes(), b'partial')

    def test_plot_uses_configured_recorder_interpreter(self):
        self.script.write_text(self.good + "import sys\nPath('interpreter.txt').write_text(sys.executable)\n")
        result = subprocess.run(
            ["bash", str(WRAPPER), "outputs/plot.py"], cwd=self.root,
            env={**os.environ, "CHART2CODE_SYSTEM_PYTHON": sys.executable},
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path((self.root / "interpreter.txt").read_text()).resolve(),
                         Path(sys.executable).resolve())

    def test_success_failure_recovery_and_association(self):
        first = self.run_render()
        self.assertEqual(first.returncode, 0, first.stderr)
        image = (self.outputs / "output.png").read_bytes()
        self.script.write_text("import sys\nprint('diagnostic')\nprint('failure', file=sys.stderr)\nsys.exit(7)\n")
        failed = self.run_render()
        self.assertEqual(failed.returncode, 7)
        self.assertEqual((self.outputs / "output.png").read_bytes(), image)
        self.assertFalse((self.outputs / "iterations/step_02.py").exists())
        self.assertIn("failure", (self.outputs / "iterations/attempt_000002/stderr.log").read_text())
        self.assertFalse(validate_render_journal(self.outputs)["passed"])
        self.script.write_text(self.good)
        self.assertEqual(self.run_render().returncode, 0)
        trajectory = {"steps": [{"step_id": 3, "assistant_content": {"tool_calls": [{"id": "call-3"}]},
            "observation": [{"toolCallId": "call-3", "content": [{"type": "text", "text":
                "[roller-render attempt_000003 status=success]"}]}]}]}
        report = validate_render_journal(self.outputs, trajectory)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["attempt_count"], 3)
        self.assertEqual(report["success_count"], 2)
        self.assertEqual(report["attempts"][2]["tool_call"]["tool_call_id"], "call-3")
        self.assertEqual(report["attempts"][0]["association"], "unmatched")
        self.assertEqual(self.rows()[1]["status"], "execution_failed")

    def test_missing_invalid_and_self_modifying_script(self):
        self.assertEqual(self.run_render().returncode, 0)
        self.script.write_text("pass\n")
        self.assertEqual(self.run_render().returncode, 0)
        self.assertEqual(self.rows()[-1]["status"], "missing_image")
        self.script.write_text("from pathlib import Path\nPath('outputs/output.png').write_bytes(b'bad')\n")
        self.assertEqual(self.run_render().returncode, 0)
        self.assertEqual(self.rows()[-1]["status"], "invalid_image")
        self.assertEqual((self.outputs / "iterations/attempt_000003/candidate.png").read_bytes(), b"bad")
        self.script.write_text(self.good + "Path(__file__).write_text('pass')\n")
        self.assertEqual(self.run_render().returncode, 0)
        self.assertEqual(self.rows()[-1]["status"], "script_changed")
        self.assertEqual((self.outputs / "output.png").read_bytes(), _png())

    def test_parallel_invocations_keep_unique_complete_pairs(self):
        self.script.write_text("import time\ntime.sleep(0.05)\n" + self.good)
        children = [subprocess.Popen(["bash", str(WRAPPER), "outputs/plot.py"], cwd=self.root,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
        for child in children:
            out, err = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, (out, err))
        self.assertEqual([row["attempt"] for row in self.rows()], [1, 2, 3])
        self.assertTrue(validate_render_journal(self.outputs)["passed"])

    def test_signal_forwarding_and_last_image_restoration(self):
        self.assertEqual(self.run_render().returncode, 0)
        self.script.write_text("from pathlib import Path\nimport time\nPath('ready').touch()\ntime.sleep(60)\n")
        child = subprocess.Popen(["bash", str(WRAPPER), "outputs/plot.py"], cwd=self.root,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not (self.root / "ready").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.root / "ready").exists())
            child.send_signal(signal.SIGTERM)
            child.communicate(timeout=5)
            self.assertEqual(child.returncode, 143)
            self.assertEqual(self.rows()[-1]["status"], "interrupted")
            self.assertEqual((self.outputs / "output.png").read_bytes(), _png())
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()

    def test_tampered_files_manifest_and_unfinished_attempt_rejected(self):
        self.assertEqual(self.run_render().returncode, 0)
        directory = self.outputs / "iterations/attempt_000001"
        (directory / "stdout.log").write_text("tampered")
        self.assertFalse(validate_render_journal(self.outputs)["passed"])
        (directory / "stdout.log").write_bytes(b"")
        self.assertTrue(validate_render_journal(self.outputs)["passed"])
        meta = directory / "metadata.json"
        original = meta.read_text()
        value = json.loads(original)
        value["status"] = "running"
        meta.write_text(json.dumps(value))
        self.assertFalse(validate_render_journal(self.outputs)["passed"])
        self.assertNotEqual(self.run_render().returncode, 0)
        meta.write_text(original)
        (self.outputs / "iterations/attempt_000002").mkdir()
        self.assertFalse(validate_render_journal(self.outputs)["passed"])

    def test_passthrough_and_symlink_rejection(self):
        result = subprocess.run(["bash", str(WRAPPER), "-c", "print('pass')"], cwd=self.root,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.stdout.strip(), "pass")
        self.assertFalse((self.outputs / "iterations").exists())
        original = self.root / "original.png"
        original.write_bytes(_png())
        (self.outputs / "output.png").symlink_to(original)
        self.assertNotEqual(self.run_render().returncode, 0)
        self.assertEqual(original.read_bytes(), _png())

    def test_bad_png_crc_and_truncated_output(self):
        for image in (_png()[:-8], _png()[:-1] + b'X'):
            self.script.write_text("from pathlib import Path\nPath('outputs/output.png').write_bytes(" + repr(image) + ")\n")
            self.assertEqual(self.run_render().returncode, 0)
            self.assertEqual(self.rows()[-1]["status"], "invalid_image")

    def test_ambiguous_marker_is_not_guessed(self):
        self.assertEqual(self.run_render().returncode, 0)
        trajectory = {"steps": [{"step_id": i, "assistant_content": {"tool_calls": [{"id": str(i)}]},
            "observation": [{"toolCallId": str(i), "content": "[roller-render attempt_000001 status=success]"}]}
            for i in (1, 2)]}
        report = validate_render_journal(self.outputs, trajectory)
        self.assertTrue(report["passed"])
        self.assertEqual(report["attempts"][0]["association"], "ambiguous")
        self.assertIsNone(report["attempts"][0]["tool_call"])

    def test_existing_legacy_steps_are_preserved(self):
        iterations = self.outputs / "iterations"
        iterations.mkdir()
        (iterations / "step_03.py").write_text("legacy")
        (iterations / "step_03.png").write_bytes(_png())
        self.assertEqual(self.run_render().returncode, 0)
        self.assertEqual(self.rows()[0]["step"], "step_04")
        self.assertEqual((iterations / "step_03.py").read_text(), "legacy")
        self.assertTrue(validate_render_journal(self.outputs)["passed"])

    def test_background_pipe_holder_does_not_hang_recorder(self):
        self.script.write_text("import subprocess\nsubprocess.Popen(['/usr/bin/python3', '-c', 'import time; time.sleep(3)'])\n" + self.good)
        result = self.run_render()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.rows()[-1]["status"], "recorder_error")


if __name__ == "__main__":
    unittest.main()
