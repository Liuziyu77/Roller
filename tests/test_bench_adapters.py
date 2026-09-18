from __future__ import annotations

import base64
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from roller.benches import BenchFormatError, binding_from_task_row
from roller.benches.base import BenchBinding, GradeSpec
from roller.benches.grading import GradedHarness
from roller.benches.skillsbench import (
    SkillsBenchAdapter,
    _portable_heredocs,
    _portable_tar_extraction,
    _qualified_from_images,
)
from roller.harnesses.base import InteractiveHarness
from roller.models import RunRequest, Task
from roller.tasks import load_tasks

class SkillsBenchAdapterTests(unittest.TestCase):
    def test_agent_failure_skips_live_container_grading(self) -> None:
        class StubHarness(InteractiveHarness):
            name = "opencode"

            def create_session(self, *_args, **_kwargs):
                raise AssertionError("session creation is not expected")

            def run(self, *_args, **_kwargs):
                raise AssertionError("harness execution is not expected")

        inner = StubHarness()
        binding = BenchBinding(
            "skillsbench",
            grade=GradeSpec("def grade(workspace_path): return {'overall_score': 1}"),
        )
        harness = GradedHarness(inner, binding)
        session = mock.Mock()
        session.container_started = True
        session.container = "already-stopped"
        request = RunRequest(0, Task("demo", ("do it",), 0), "model", 30)

        outcome = harness._grade(session, request, "timeout")

        assert outcome is not None
        self.assertEqual(outcome.scores["overall_score"], 0.0)
        self.assertFalse(outcome.scores["score_eligible"])
        self.assertIn("grading skipped", outcome.error)

    def _root(self, temporary: str) -> Path:
        root = Path(temporary)
        task = root / "tasks" / "demo"
        environment = task / "environment"
        skills = environment / "skills" / "demo-skill"
        verifier = task / "verifier"
        skills.mkdir(parents=True)
        verifier.mkdir()
        (skills / "SKILL.md").write_text("Use the demo process.\n", encoding="utf-8")
        (environment / "seed.txt").write_text("seed\n", encoding="utf-8")
        (environment / "Dockerfile").write_text(
            """FROM ubuntu:24.04
ENV WORKSPACE=/work
WORKDIR ${WORKSPACE}
COPY seed.txt /work/seed.txt
COPY skills /root/.opencode/skill
""",
            encoding="utf-8",
        )
        (verifier / "test.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (task / "task.md").write_text(
            """---
metadata:
  category: demo-category
  difficulty: easy
agent:
  timeout_sec: 30
verifier:
  timeout_sec: 20
  env:
    EXPECTED: yes
sandbox:
  build_timeout_sec: 60
  CUSTOM_ARG: value
---

Complete the demo.
""",
            encoding="utf-8",
        )
        task_set = root / "integrations" / "agentbeats" / "task_sets"
        task_set.mkdir(parents=True)
        (task_set / "skillsbench-v1.1.json").write_text(
            json.dumps(
                {
                    "tasks": [
                        {
                            "task_id": "demo",
                            "category": "manifest-category",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return root

    def test_with_skill_and_no_skill_are_isolated_runtime_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            adapter = SkillsBenchAdapter()
            with_skill = adapter.load(root, variant="with-skill")[0]
            no_skill = adapter.load(root, variant="no-skill")[0]

            self.assertEqual(with_skill["harness"], "opencode")
            self.assertEqual(with_skill["runtime"]["workspace_path"], "/work")
            self.assertIn("context", with_skill["files"])
            self.assertNotIn("context", no_skill["files"])
            self.assertEqual(
                with_skill["runtime"]["image"], no_skill["runtime"]["image"]
            )
            dockerfile = with_skill["runtime"]["image_build"]["dockerfile"]
            self.assertNotIn("COPY skills", dockerfile)
            self.assertEqual(
                with_skill["runtime"]["image_build"]["args"],
                {"CUSTOM_ARG": "value"},
            )
            binding = binding_from_task_row(with_skill)
            assert binding is not None and binding.grade is not None
            self.assertEqual(binding.category, "manifest-category")
            self.assertEqual(
                binding.grade.stage,
                ((str((root / "tasks" / "demo" / "verifier").resolve()), "verifier"),),
            )
            self.assertEqual(binding.grade.kwargs["task_workspace"], "/work")
            self.assertIn('Path("/tests").mkdir', binding.grade.code)
            self.assertIn("reward_path.parent.mkdir", binding.grade.code)
            self.assertIn('local_bin / "env"', binding.grade.code)
            self.assertIn("for verifier_attempts in range(1, 4)", binding.grade.code)
            self.assertIn('"verifier_attempts": verifier_attempts', binding.grade.code)
            self.assertIn("tempfile.NamedTemporaryFile", binding.grade.code)
            self.assertIn("stderr=subprocess.STDOUT", binding.grade.code)
            self.assertNotIn("capture_output=True", binding.grade.code)

            exported = root / "skills.jsonl"
            exported.write_text(json.dumps(with_skill) + "\n", encoding="utf-8")
            task = load_tasks(exported)[0]
            self.assertEqual(task.docker_image, with_skill["runtime"]["image"])
            assert task.image_build is not None
            self.assertEqual(task.image_build.harness, "opencode")
            self.assertEqual(task.workspace_container_path, "/work")

    def test_aliases_and_bad_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            adapter = SkillsBenchAdapter()
            self.assertEqual(
                adapter.load(root, variant="without-skills")[0]["bench"]["metadata"]["variant"],
                "no-skill",
            )
            with self.assertRaises(BenchFormatError):
                adapter.load(root, task_ids=["missing"])
            with self.assertRaises(BenchFormatError):
                adapter.load(root, variant="unknown")

    def test_timeout_multiplier_scales_skillsbench_agent_and_verifier_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            row = SkillsBenchAdapter().load(
                self._root(temporary), timeout_multiplier=1.5
            )[0]
        self.assertEqual(row["timeout_seconds"], 45)
        binding = binding_from_task_row(row)
        assert binding is not None and binding.grade is not None
        self.assertEqual(binding.grade.timeout_seconds, 60)
        self.assertIn("timeout=30", binding.grade.code)

    def test_skillsbench_verifier_timeout_multiplier_is_not_capped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            row = SkillsBenchAdapter().load(
                self._root(temporary), timeout_multiplier=200
            )[0]
        binding = binding_from_task_row(row)
        assert binding is not None and binding.grade is not None
        self.assertEqual(binding.grade.timeout_seconds, 4030)
        self.assertIn("timeout=4000", binding.grade.code)

    def test_official_v1_1_environment_metadata_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            task_file = root / "tasks" / "demo" / "task.md"
            task_file.write_text(
                task_file.read_text(encoding="utf-8").replace(
                    "sandbox:\n", "environment:\n"
                ),
                encoding="utf-8",
            )
            row = SkillsBenchAdapter().load(root)[0]

        self.assertEqual(row["runtime"]["image_build"]["args"], {"CUSTOM_ARG": "value"})
        self.assertEqual(
            row["bench"]["metadata"]["resource_request"]["build_timeout_sec"],
            60,
        )

    def test_aggregate_excludes_ineligible_timeout_and_infrastructure_rows(self) -> None:
        rows = [
            {
                "task_id": "pass",
                "status": "ok",
                "bench_category": "demo",
                "bench_scores": {
                    "overall_score": 1.0,
                    "passed": True,
                    "score_eligible": True,
                },
            },
            {
                "task_id": "model-failure",
                "status": "ok",
                "bench_category": "demo",
                "bench_scores": {
                    "overall_score": 0.0,
                    "passed": False,
                    "score_eligible": True,
                },
            },
            {
                "task_id": "timeout",
                "status": "timeout",
                "bench_category": "demo",
                "bench_scores": {"overall_score": 1.0},
            },
            {
                "task_id": "broken-verifier",
                "status": "ok",
                "bench_category": "demo",
                "bench_scores": {
                    "overall_score": 0.0,
                    "verifier_output": "/bin/sh: uvx: command not found",
                },
            },
            {
                "task_id": "proxied-localhost",
                "status": "ok",
                "bench_category": "demo",
                "bench_scores": {
                    "overall_score": 0.0,
                    "verifier_output": "Squid response: ERR_ACCESS_DENIED",
                },
            },
            {
                "task_id": "download-recovered",
                "status": "ok",
                "bench_category": "demo",
                "bench_scores": {
                    "overall_score": 1.0,
                    "passed": True,
                    "verifier_output": (
                        "failed to download optional bootstrap\n"
                        "test session starts\nshort test summary info\n1 passed in 0.1s"
                    ),
                },
            },
        ]

        report = SkillsBenchAdapter().aggregate(rows)

        self.assertEqual(report["task_count"], 6)
        self.assertEqual(report["scored_count"], 3)
        self.assertEqual(report["unscored_count"], 3)
        self.assertEqual(report["passed_count"], 2)
        self.assertEqual(report["pass_rate"], 0.666667)
        self.assertEqual(report["mean_reward"], 0.666667)
        self.assertEqual(report["categories"]["demo"]["task_count"], 3)

    def test_dockerfile_heredocs_are_rewritten_for_podman(self) -> None:
        source = """FROM python:3.12-slim
RUN python - <<'PY'
from pathlib import Path
Path('/tmp/value').write_text("ok")
PY
RUN mkdir -p /tmp/demo && \\
    cat > /tmp/demo/value.xml <<'XML'
<value>ok</value>
XML
RUN <<'EOF'
set -eu
echo ready
EOF
RUN <<'BASH'
set -euo pipefail
echo bash-ready
BASH
"""

        portable = _portable_heredocs(source)

        self.assertNotIn("<<", portable)
        self.assertIn("| python -", portable)
        self.assertIn("> /tmp/demo/value.xml", portable)
        self.assertIn("| base64 -d | python -", portable)
        self.assertIn("| base64 -d | /bin/sh", portable)
        self.assertIn("| base64 -d | /bin/bash", portable)
        self.assertNotIn("from pathlib import Path", portable)

        seisbench = _portable_heredocs(
            "RUN python - <<'PY'\n"
            "import seisbench.models as sbm\n"
            'sbm.PhaseNet.from_pretrained("instance")\n'
            "PY\n"
        )
        self.assertIn("pip install --no-cache-dir 'setuptools<81'", seisbench)
        decoded = base64.b64decode(
            re.search(r"printf %s ([A-Za-z0-9+/=]+)", seisbench).group(1)
        ).decode("utf-8")
        self.assertIn("use_backup_repository", decoded)
        compile(decoded, "<generated-preload>", "exec")

    def test_v1_1_pycbc_environment_gets_upstream_compatibility_pin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dockerfile = Path(temporary) / "Dockerfile"
            dockerfile.write_text(
                'FROM python:3.12\nRUN pip install \\\n'
                '    "setuptools<81" \\\n'
                "    pycbc==2.8.5\n",
                encoding="utf-8",
            )
            from roller.benches.skillsbench import _sanitized_dockerfile

            sanitized = _sanitized_dockerfile(dockerfile)

        self.assertIn('"astropy==7.2.1"', sanitized)

    def test_v1_1_coursier_setup_uses_pinned_launcher_directly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dockerfile = Path(temporary) / "Dockerfile"
            dockerfile.write_text(
                "FROM ubuntu:24.04\n"
                "RUN curl -fL https://github.com/coursier/coursier/releases/download/"
                "v2.1.25-M23/cs-x86_64-pc-linux.gz | gzip -d > cs && "
                "chmod +x cs && ./cs setup --yes && "
                "./cs install scala:2.13.12 scalac:2.13.12\n",
                encoding="utf-8",
            )
            from roller.benches.skillsbench import _sanitized_dockerfile

            sanitized = _sanitized_dockerfile(dockerfile)

        self.assertIn("--retry 5 --retry-all-errors --retry-delay 5", sanitized)
        self.assertNotIn("./cs setup", sanitized)
        self.assertIn("./cs install scala:2.13.12", sanitized)

    def test_unterminated_dockerfile_heredoc_is_rejected(self) -> None:
        with self.assertRaisesRegex(BenchFormatError, "unterminated"):
            _portable_heredocs("FROM python:3.12-slim\nRUN python - <<'PY'\nprint(1)\n")

    def test_docker_hub_namespaces_are_explicit_for_podman(self) -> None:
        source = """FROM --platform=linux/amd64 bugswarm/cached-images:tag AS source
FROM jasonish/suricata:7.0.11
COPY --from=source /tmp/value /tmp/value
FROM gcr.io/example/image:tag AS final
"""

        portable = _qualified_from_images(source)

        self.assertIn("FROM --platform=linux/amd64 docker.io/bugswarm/", portable)
        self.assertIn("FROM docker.io/jasonish/suricata:7.0.11", portable)
        self.assertIn("FROM gcr.io/example/image:tag AS final", portable)

    def test_tar_extraction_ignores_archive_ownership_in_rootless_builds(self) -> None:
        source = """FROM ubuntu:24.04
RUN tar -xzf /tmp/traces.tar.gz -C /root/traces
RUN curl example.test/a.tgz | tar -xz -C /opt/value
RUN mkdir -p /root/traces && \\
    tar -xzf /tmp/continued.tar.gz -C /root/traces
RUN apt-get install -y \\
    tar \\
    xz-utils
RUN tar --no-same-owner -xf /tmp/already-safe.tar
RUN echo tar -xzf is documentation
"""

        portable = _portable_tar_extraction(source)

        self.assertIn(
            "tar --no-same-owner --no-same-permissions -xzf", portable
        )
        self.assertIn(
            "| tar --no-same-owner --no-same-permissions -xz", portable
        )
        self.assertIn(
            "    tar --no-same-owner --no-same-permissions -xzf "
            "/tmp/continued.tar.gz",
            portable,
        )
        self.assertIn("tar --no-same-owner -xf /tmp/already-safe.tar", portable)
        self.assertIn("    tar \\\n    xz-utils", portable)
        self.assertIn("RUN echo tar -xzf is documentation", portable)


if __name__ == "__main__":
    unittest.main()
