"""Host-side orchestration for scoring in a fresh official instance."""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import time
import uuid
from importlib import resources
from pathlib import Path
from typing import Any

from roller.benches.grading import ContainerGrader, GradedHarness, GradeOutcome
from roller.benches.swebench_pro.constants import (
    BASELINE_REF,
    OUTPUT_DIR_NAME,
    WORKSPACE_CONTAINER_PATH,
)
from roller.benches.swebench_pro.images import _image_id
from roller.benches.swebench_pro.workspace import _setup_command

logger = logging.getLogger(__name__)

_GRADER_SOURCE = "\n" + resources.files("roller.benches.swebench_pro").joinpath(
    "grader.py"
).read_text(encoding="utf-8")


def _strip_binary_hunks(patch: str) -> str:
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    return "".join(
        section
        for section in sections
        if section.strip()
        and not re.search(r"^Binary files .* differ$", section, re.MULTILINE)
        and not re.search(r"^GIT binary patch$", section, re.MULTILINE)
    )


class SWEBenchProGradedHarness(GradedHarness):
    """Extract an Agent patch, then score it in a fresh official instance."""

    def _extract_patch(self, session: Any) -> tuple[str | None, str | None]:
        script = (
            f"git add -N -- . && git diff --binary --full-index --no-ext-diff "
            f"{BASELINE_REF} -- ."
        )
        try:
            result = getattr(self.inner, "runtime").run(
                [
                    "exec",
                    "-w",
                    WORKSPACE_CONTAINER_PATH,
                    session.container,
                    "/bin/bash",
                    "-lc",
                    script,
                ],
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            return None, "model patch extraction timed out"
        if result.returncode:
            detail = (result.stderr or result.stdout or "").strip()[-2000:]
            return None, f"model patch extraction failed: {detail}"
        return _strip_binary_hunks(result.stdout or ""), None

    def _copy_grade_outputs(
        self, runtime: Any, grader_container: str, agent_container: str
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="roller-swebench-results-"
        ) as temporary:
            staged = Path(temporary)
            copied = runtime.run(
                [
                    "cp",
                    f"{grader_container}:{WORKSPACE_CONTAINER_PATH}/{OUTPUT_DIR_NAME}/.",
                    str(staged),
                ],
                timeout=120,
            )
            if copied.returncode:
                return
            runtime.run(
                [
                    "exec",
                    agent_container,
                    "mkdir",
                    "-p",
                    f"{WORKSPACE_CONTAINER_PATH}/{OUTPUT_DIR_NAME}",
                ],
                timeout=30,
            )
            runtime.run(
                [
                    "cp",
                    f"{staged}/.",
                    f"{agent_container}:{WORKSPACE_CONTAINER_PATH}/{OUTPUT_DIR_NAME}",
                ],
                timeout=120,
            )

    def _grade(self, session: Any, request: Any, status: str) -> GradeOutcome | None:
        if self.grading_semaphore is None:
            return self._grade_in_fresh_container(session, request, status)
        with self.grading_semaphore:
            return self._grade_in_fresh_container(session, request, status)

    def _grade_in_fresh_container(
        self, session: Any, request: Any, status: str
    ) -> GradeOutcome | None:
        started = time.monotonic()
        spec = self.binding.grade
        if spec is None:
            return None
        if not getattr(session, "container_started", False):
            return super()._grade(session, request, status)
        patch, error = self._extract_patch(session)
        if error is not None:
            return GradeOutcome(
                {"overall_score": 0.0, "resolved": 0, "error": error},
                time.monotonic() - started,
                error,
            )

        runtime = getattr(self.inner, "runtime")
        source_image = self.binding.metadata["source_image"]
        base_commit = self.binding.metadata["base_commit"]
        _image_id(runtime, source_image)
        grader_container = f"roller-swebench-grade-{uuid.uuid4().hex[:12]}"
        try:
            launched = runtime.run(
                [
                    "run",
                    "-d",
                    "--name",
                    grader_container,
                    "--label",
                    "ai.roller.role=swebench-pro-grader",
                    "--user",
                    "0",
                    "--entrypoint",
                    "/bin/bash",
                    source_image,
                    "-c",
                    "tail -f /dev/null",
                ],
                timeout=60,
            )
            if launched.returncode:
                detail = (launched.stderr or launched.stdout or "").strip()[-2000:]
                raise RuntimeError(f"fresh grading container startup failed: {detail}")
            prepared = runtime.run(
                [
                    "exec",
                    "-w",
                    WORKSPACE_CONTAINER_PATH,
                    grader_container,
                    "/bin/bash",
                    "-lc",
                    _setup_command(
                        base_commit, go_proxy=self.binding.metadata.get("go_proxy")
                    ),
                ],
                timeout=300,
            )
            if prepared.returncode:
                detail = (prepared.stderr or prepared.stdout or "").strip()[-2000:]
                raise RuntimeError(f"fresh grading baseline setup failed: {detail}")
            with tempfile.NamedTemporaryFile(
                "w", suffix=".patch", delete=False, encoding="utf-8"
            ) as handle:
                handle.write(patch or "")
                patch_path = Path(handle.name)
            try:
                copied = runtime.run(
                    ["cp", str(patch_path), f"{grader_container}:/tmp/model.patch"],
                    timeout=60,
                )
                if copied.returncode:
                    raise RuntimeError("model patch copy to fresh grader failed")
            finally:
                patch_path.unlink(missing_ok=True)
            if patch:
                applied = runtime.run(
                    [
                        "exec",
                        "-w",
                        WORKSPACE_CONTAINER_PATH,
                        grader_container,
                        "git",
                        "apply",
                        "--verbose",
                        "/tmp/model.patch",
                    ],
                    timeout=120,
                )
                if applied.returncode:
                    detail = (applied.stderr or applied.stdout or "").strip()[-2000:]
                    raise RuntimeError(
                        f"model patch did not apply in fresh grader: {detail}"
                    )
            outcome = ContainerGrader(spec).grade(
                runtime,
                grader_container,
                task_id=request.task.id,
                workspace_path=WORKSPACE_CONTAINER_PATH,
                transcript_container_path=None,
            )
            self._copy_grade_outputs(runtime, grader_container, session.container)
            return outcome
        except Exception as exc:  # noqa: BLE001 - grading failures become score metadata
            return GradeOutcome(
                {"overall_score": 0.0, "resolved": 0, "error": str(exc)},
                time.monotonic() - started,
                str(exc),
            )
        finally:
            try:
                cleanup = runtime.run(["rm", "-f", grader_container], timeout=30)
                if cleanup.returncode:
                    detail = (cleanup.stderr or cleanup.stdout or "").strip()[-500:]
                    logger.warning(
                        "[%s] grader container cleanup failed for %s%s",
                        request.task.id,
                        grader_container,
                        f": {detail}" if detail else "",
                    )
            except Exception:  # noqa: BLE001 - cleanup must not mask grading
                logger.warning(
                    "[%s] grader container cleanup raised for %s",
                    request.task.id,
                    grader_container,
                    exc_info=True,
                )
