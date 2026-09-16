from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from roller.benches.base import BenchBinding, BenchFormatError, GradeSpec
from roller.benches.grading import GradedHarness, GradeOutcome
from roller.benches.swebench_pro import (
    _DERIVED_IMAGES,
    _GRADER_SOURCE,
    SWEBenchProAdapter,
    SWEBenchProGradedHarness,
)
from roller.harnesses.hermes import HermesConfig, HermesHarness
from roller.harnesses.openclaw import OpenClawConfig, OpenClawHarness


def _record(instance_id: str = "instance_owner__repo-deadbeef-v1") -> dict[str, str]:
    return {
        "repo": "owner/repo",
        "instance_id": instance_id,
        "base_commit": "a" * 40,
        "patch": "GOLD SOURCE PATCH MUST NOT LEAK",
        "test_patch": (
            "diff --git a/tests/hidden.txt b/tests/hidden.txt\n"
            "index 8d1a494..a6a0371 100644\n"
            "--- a/tests/hidden.txt\n"
            "+++ b/tests/hidden.txt\n"
            "@@ -1 +1 @@\n"
            "-BASE\n"
            "+HIDDEN\n"
        ),
        "problem_statement": '"Fix the implementation.\\nKeep compatibility."',
        "requirements": '"- Return the expected value."',
        "interface": '"Type: Function\\nName: solve"',
        "repo_language": "python",
        "fail_to_pass": "['required_test']",
        "pass_to_pass": "['regression_test']",
        "issue_specificity": '["major_bug"]',
        "issue_categories": '["back_end_knowledge"]',
        "before_repo_set_cmd": (
            f"git reset --hard {'a' * 40}\n"
            f"git checkout {'a' * 40}\n"
            "git checkout deadbeef -- tests/hidden.txt"
        ),
        "selected_test_files_to_run": '["tests/hidden.txt"]',
        "dockerhub_tag": "owner.repo-owner__repo-deadbeef-v1",
    }


def _write_fixture(root: Path, record: dict[str, str] | None = None) -> dict[str, str]:
    row = record or _record()
    (root / "test.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    scripts = root / "run_scripts" / row["instance_id"]
    scripts.mkdir(parents=True)
    (scripts / "run_script.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    (scripts / "parser.py").write_text("pass\n", encoding="utf-8")
    return row


class SWEBenchProAdapterTests(unittest.TestCase):
    def test_load_exports_prompt_and_hides_gold_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = _write_fixture(root)
            row = SWEBenchProAdapter(cache_root=root / "cache").load(root)[0]

            self.assertEqual(row["id"], record["instance_id"])
            self.assertIn("Requirements:\n- Return the expected value.", row["prompt"])
            self.assertNotIn("GOLD SOURCE PATCH MUST NOT LEAK", json.dumps(row))
            self.assertIn("rm -rf .git", row["runtime"]["setup"][0])
            binding = BenchBinding.from_dict(row["bench"])
            assert binding.grade is not None
            self.assertEqual(binding.grade.workspace_path, "/app")
            self.assertFalse(binding.grade.needs_transcript)
            self.assertEqual(len(binding.grade.stage), 3)

    def test_load_rejects_unknown_and_unsafe_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_fixture(root)
            adapter = SWEBenchProAdapter(cache_root=root / "cache")
            with self.assertRaisesRegex(
                BenchFormatError, "unknown SWE-Bench Pro task ids"
            ):
                adapter.load(root, task_ids=["missing"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_fixture(root, _record("../escape"))
            with self.assertRaisesRegex(BenchFormatError, "unsafe instance_id"):
                SWEBenchProAdapter(cache_root=root / "cache").load(root)


class _FakeDockerRuntime:
    def __init__(self, harness_image: str) -> None:
        self.images = {harness_image: "sha256:harness"}
        self.dockerfile = ""

    def run(self, args, timeout=None):
        del timeout
        arguments = list(args)
        if arguments[:2] == ["image", "inspect"]:
            image_id = self.images.get(arguments[2])
            return subprocess.CompletedProcess(
                arguments, 0 if image_id else 1, f"{image_id}\n" if image_id else "", ""
            )
        if arguments[:1] == ["pull"]:
            self.images[arguments[1]] = "sha256:source"
            return subprocess.CompletedProcess(arguments, 0, "pulled\n", "")
        if arguments[:1] == ["build"]:
            self.dockerfile = (Path(arguments[-1]) / "Dockerfile").read_text()
            return subprocess.CompletedProcess(arguments, 0, "built\n", "")
        raise AssertionError(arguments)


class SWEBenchProImageTests(unittest.TestCase):
    def test_wrap_openclaw_builds_instance_image(self) -> None:
        suffix = uuid.uuid4().hex
        source = f"registry.example/source:{suffix}"
        image = f"registry.example/openclaw:{suffix}"
        runtime = _FakeDockerRuntime(image)
        harness = OpenClawHarness(OpenClawConfig(docker_image=image), 3, runtime)
        binding = BenchBinding(
            name="swebench-pro",
            metadata={"source_image": source, "base_commit": "a" * 40},
            grade=GradeSpec(code="def grade(workspace_path): return {}"),
        )
        _DERIVED_IMAGES.clear()

        wrapped = SWEBenchProAdapter().wrap_harness(harness, binding)

        self.assertIsInstance(wrapped, GradedHarness)
        self.assertEqual(wrapped.inner.config.workspace_container_path, "/app")
        self.assertIn(f"FROM {source}", runtime.dockerfile)
        self.assertIn(f"FROM {image} AS harness", runtime.dockerfile)
        self.assertIn("ENTRYPOINT []", runtime.dockerfile)

    def test_wrap_non_openclaw_copies_repo_into_harness_image(self) -> None:
        suffix = uuid.uuid4().hex
        source = f"registry.example/source:{suffix}"
        image = f"registry.example/hermes:{suffix}"
        runtime = _FakeDockerRuntime(image)
        harness = HermesHarness(HermesConfig(docker_image=image), 2, runtime)
        binding = BenchBinding(
            name="swebench-pro",
            metadata={"source_image": source, "base_commit": "a" * 40},
            grade=GradeSpec(code="def grade(workspace_path): return {}"),
        )
        _DERIVED_IMAGES.clear()

        wrapped = SWEBenchProAdapter().wrap_harness(harness, binding)

        self.assertIsInstance(wrapped.inner, HermesHarness)
        self.assertEqual(wrapped.inner.config.workspace_container_path, "/app")
        self.assertIn(f"FROM {source} AS instance", runtime.dockerfile)
        self.assertIn(f"FROM {image}", runtime.dockerfile)
        self.assertIn("COPY --from=instance /app /app", runtime.dockerfile)


class SWEBenchProGraderTests(unittest.TestCase):
    def _git(self, workspace: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=workspace, capture_output=True, text=True, check=True
        ).stdout

    def test_grader_extracts_committed_patch_and_restores_hidden_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            self._git(workspace, "init", "-q")
            self._git(workspace, "config", "user.name", "Roller Test")
            self._git(workspace, "config", "user.email", "roller@test.invalid")
            (workspace / "app.py").write_text("value = 'base'\n")
            tests = workspace / "tests"
            tests.mkdir()
            (tests / "hidden.txt").write_text("BASE\n")
            self._git(workspace, "add", ".")
            self._git(workspace, "commit", "-qm", "baseline")
            self._git(workspace, "update-ref", "refs/roller/baseline", "HEAD")
            with (workspace / ".git/info/exclude").open("a") as handle:
                handle.write(".roller-grade/\n.roller_outputs/\n")
            (tests / "hidden.txt").write_text("HIDDEN\n")
            hidden_patch = self._git(workspace, "diff", "--", "tests/hidden.txt")
            self._git(workspace, "checkout", "--", "tests/hidden.txt")
            (workspace / "app.py").write_text("value = 'model-fixed'\n")
            (tests / "hidden.txt").write_text("AGENT-CHEAT\n")
            self._git(workspace, "add", ".")
            self._git(workspace, "commit", "-qm", "agent commit")

            grade_dir = workspace / ".roller-grade"
            grade_dir.mkdir()
            (grade_dir / "test.patch").write_text(hidden_patch)
            (grade_dir / "run_script.sh").write_text(
                "#!/bin/bash\n"
                "if grep -q model-fixed app.py && grep -q HIDDEN tests/hidden.txt; then\n"
                " echo 'required_test PASSED'; echo 'regression_test PASSED'\n"
                "else echo 'required_test FAILED'; fi\n"
            )
            (grade_dir / "parser.py").write_text(
                "import json,pathlib,sys\n"
                "tests=[]\n"
                "for line in pathlib.Path(sys.argv[1]).read_text().splitlines():\n"
                " name,status=line.split(); tests.append({'name':name,'status':status})\n"
                "pathlib.Path(sys.argv[3]).write_text(json.dumps({'tests':tests}))\n"
            )
            namespace = {}
            exec(_GRADER_SOURCE, namespace)  # noqa: S102
            scores = namespace["roller_grade"](
                str(workspace),
                ["required_test"],
                ["regression_test"],
                ["tests/hidden.txt"],
                ["tests/hidden.txt"],
                30,
            )

            self.assertEqual(scores["overall_score"], 1.0)
            patch = (workspace / ".roller_outputs/model.patch").read_text()
            self.assertIn("model-fixed", patch)
            self.assertEqual((tests / "hidden.txt").read_text(), "HIDDEN\n")

    def test_fresh_grader_container_is_used_and_always_removed(self) -> None:
        class RecordingRuntime:
            def __init__(self) -> None:
                self.calls: list[list[str]] = []

            def run(self, args, timeout=None):
                del timeout
                arguments = list(args)
                self.calls.append(arguments)
                stdout = (
                    "sha256:source\n" if arguments[:2] == ["image", "inspect"] else ""
                )
                return subprocess.CompletedProcess(arguments, 0, stdout, "")

        runtime = RecordingRuntime()
        inner = HermesHarness(
            HermesConfig(docker_image="registry.example/hermes:test"),
            1,
            runtime,
        )
        binding = BenchBinding(
            name="swebench-pro",
            metadata={
                "source_image": "registry.example/source:test",
                "base_commit": "a" * 40,
            },
            grade=GradeSpec(code="def roller_grade(workspace_path): return {}"),
        )
        harness = SWEBenchProGradedHarness(inner, binding)
        session = SimpleNamespace(container_started=True, container="agent-container")
        request = SimpleNamespace(task=SimpleNamespace(id="instance-test"))
        expected = GradeOutcome({"overall_score": 1.0, "resolved": 1}, 0.1)

        with patch.object(
            harness, "_extract_patch", return_value=("diff --git a/a b/a\n", None)
        ):
            with patch(
                "roller.benches.swebench_pro.ContainerGrader.grade",
                return_value=expected,
            ) as grade:
                outcome = harness._grade(session, request, "ok")

        self.assertIs(outcome, expected)
        run_call = next(call for call in runtime.calls if call[:2] == ["run", "-d"])
        grader_container = run_call[run_call.index("--name") + 1]
        self.assertIn("ai.roller.role=swebench-pro-grader", run_call)
        self.assertEqual(
            run_call[-3:], ["registry.example/source:test", "-c", "tail -f /dev/null"]
        )
        grade.assert_called_once()
        self.assertEqual(grade.call_args.args[1], grader_container)
        self.assertIn(["rm", "-f", grader_container], runtime.calls)


if __name__ == "__main__":
    unittest.main()
