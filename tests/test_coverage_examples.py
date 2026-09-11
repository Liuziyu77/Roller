"""Public coverage examples must work after cloning, without private fixtures."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]


class CoverageExamplesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="roller-public-examples-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.checkout = Path(cls.temporary.name) / "checkout 中文 with spaces"
        cls.coverage = cls.checkout / "examples" / "coverage"
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".env", "*.local.json", "local", "reports", "results", "live-test")
        shutil.copytree(PROJECT / "examples" / "coverage", cls.coverage, ignore=ignore)
        shutil.copytree(PROJECT / "src", cls.checkout / "src", ignore=ignore)
        cls.env = {
            **os.environ,
            "PYTHONPATH": str(cls.checkout / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            # A maintainer's environment must not leak into generated templates.
            "ROLLER_TEST_CODEX_MODEL": "local-model-must-not-be-published",
        }

    def run_script(self, script, *args, success=True):
        result = subprocess.run(
            [sys.executable, str(self.coverage / script), *map(str, args)],
            cwd=self.checkout, env=self.env, text=True, capture_output=True,
            timeout=90,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_regeneration_is_portable_and_independent_of_environment(self):
        def snapshot():
            return {
                path.relative_to(self.coverage).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.coverage.rglob("*") if path.is_file()
            }

        before = snapshot()
        self.assertFalse((self.checkout.parent / "Roller_Data").exists())
        self.run_script("generate.py")
        self.run_script("merge.py")
        self.assertEqual(snapshot(), before)
        for path in self.coverage.rglob("*"):
            if path.is_file():
                with self.subTest(path=path.relative_to(self.coverage)):
                    self.assertNotIn(str(PROJECT).encode(), path.read_bytes())
                    self.assertNotIn(str(self.checkout).encode(), path.read_bytes())

    def test_relocated_matrix_and_runtime_absolute_roots(self):
        self.run_script("suite.py", "validate")
        self.run_script("validate_merged.py")
        self.run_script("suite.py", "run", "--case", "inputs/*/codex", "--dry-run")
        output = self.checkout / "runs" / "absolute" / "tasks.json"
        self.run_script("merge.py", "--case", "inputs/absolute_root/*", "--absolute-roots", "--output", output)
        payload = json.loads(output.read_text())
        self.assertEqual(len(payload["tasks"]), 12)
        for row in payload["tasks"]:
            self.assertEqual(Path(row["file_root"]), self.coverage / "fixtures")
        self.run_script("validate_merged.py", "--tasks", output)

    def test_subset_binds_all_simulator_pairs_and_preserves_inheritance(self):
        output = self.checkout / "runs" / "mixed" / "tasks.json"
        models = {"openclaw": "example/provider-model", "codex": "example-codex", "claude-code": "example-claude"}
        model_args = [arg for name, value in models.items() for arg in ("--" + name + "-model", value)]
        self.run_script("merge.py", "--case", "simulators/pair/*", *model_args, "--output", output)
        payload = json.loads(output.read_text())
        self.assertEqual(payload["models"], models)
        self.assertEqual(len(payload["tasks"]), 36)
        self.assertEqual(len(payload["negative_cases"]), 20)
        inheritance = 0
        pairs = set()
        for index, (row, coverage) in enumerate(zip(payload["tasks"], payload["coverage"])):
            agent, simulator = row["harness"], row["user_simulator"]
            pairs.add((agent, simulator["harness"]))
            self.assertEqual(row["model"], models[agent])
            self.assertEqual(coverage["source_index"], index)
            self.assertEqual(coverage["task_id"], row["id"])
            if "model" in simulator:
                self.assertEqual(simulator["model"], models[simulator["harness"]])
            else:
                inheritance += 1
                self.assertEqual(agent, simulator["harness"])
        self.assertEqual(len(pairs), 9)
        self.assertEqual(inheritance, 3)
        self.run_script("validate_merged.py", "--tasks", output)

    def test_local_model_cannot_overwrite_public_template(self):
        path = self.coverage / "all_tasks.json"
        original = path.read_bytes()
        self.run_script("merge.py", "--model", "local-model", success=False)
        self.run_script("merge.py", "--case", "formats/jsonl/codex", success=False)
        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
