"""QwenClawBench adapter assembly."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from roller.benches.base import BenchAdapter, BenchBinding, BenchFormatError, GradeSpec
from roller.benches.qwenclaw.grader_runtime import (
    GRADE_TIMEOUT_SECONDS,
    JUDGE_ENV_NAMES,
    OUTPUT_DIR_NAME,
    WORKSPACE_CONTAINER_PATH,
    QwenClawGradedHarness,
    build_grader_source,
)
from roller.benches.qwenclaw.report import aggregate_samples
from roller.benches.qwenclaw.task_parser import parse_task_markdown
from roller.benches.qwenclaw.workspace import materialize_workspace

TASK_SCHEMA = "roller.task/v1"
DATASET_DIR_NAME = "qwenclawbench-v1.1-100"


class QwenClawBenchAdapter(BenchAdapter):
    name = "qwenclawbench"
    description = "QwenClawBench — 100 real-user OpenClaw tasks across 8 domains"

    def __init__(self, cache_root: Path | None = None) -> None:
        self.cache_root = cache_root

    def load(
        self,
        root: Path,
        *,
        categories: Sequence[str] | None = None,
        task_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        dataset = self._dataset_root(root)
        tasks_root = dataset / "tasks"
        assets_root = dataset / "assets"
        wanted_categories = set(categories or ())
        wanted_ids = set(task_ids or ())
        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        available_categories: set[str] = set()

        for task_path in sorted(tasks_root.glob("task_*.md")):
            task = parse_task_markdown(
                task_path.read_text(encoding="utf-8"),
                str(task_path),
                source_path=task_path,
            )
            if task.task_id in seen_ids:
                raise BenchFormatError(
                    f"duplicate QwenClawBench task id: {task.task_id}"
                )
            seen_ids.add(task.task_id)
            if task.category:
                available_categories.add(task.category)
            if wanted_categories and task.category not in wanted_categories:
                continue
            if wanted_ids and task.task_id not in wanted_ids:
                continue

            workspace = materialize_workspace(task, assets_root, self.cache_root)
            env_names = (
                JUDGE_ENV_NAMES if task.grading_type in {"llm_judge", "hybrid"} else ()
            )
            binding = BenchBinding(
                name=self.name,
                category=task.category or None,
                metadata={
                    "upstream_task_id": task.task_id,
                    "name": task.name,
                    "subcategory": task.subcategory,
                    "grading_type": task.grading_type,
                    "grading_weights": task.grading_weights,
                    "input_modality": task.input_modality,
                    "external_dependency": task.external_dependency,
                    "run_index": 1,
                    "runs_per_task": 1,
                },
                grade=GradeSpec(
                    code=build_grader_source(task),
                    entrypoint="roller_grade",
                    env_names=env_names,
                    timeout_seconds=GRADE_TIMEOUT_SECONDS,
                    needs_transcript=True,
                    workspace_path=WORKSPACE_CONTAINER_PATH,
                    score_key="overall_score",
                ),
            )
            rows.append(
                {
                    "schema_version": TASK_SCHEMA,
                    "id": task.task_id,
                    "harness": "openclaw",
                    "prompt": task.prompt,
                    "workspace": str(workspace),
                    "files": {"output_dir": OUTPUT_DIR_NAME},
                    "timeout_seconds": task.timeout_seconds,
                    "bench": binding.as_dict(),
                }
            )

        missing_categories = wanted_categories - available_categories
        if missing_categories:
            raise BenchFormatError(
                "unknown QwenClawBench categories: "
                + ", ".join(sorted(missing_categories))
            )
        missing_ids = wanted_ids - seen_ids
        if missing_ids:
            raise BenchFormatError(
                "unknown QwenClawBench task ids: " + ", ".join(sorted(missing_ids))
            )
        return rows

    def wrap_harness(self, harness, binding):
        if binding.grade is None:
            return harness
        grade = replace(
            binding.grade, transcript_path="/tmp/roller-qwenclaw-transcript.json"
        )
        return QwenClawGradedHarness(harness, replace(binding, grade=grade))

    def aggregate(self, samples: Iterable[dict[str, Any]]) -> dict[str, Any]:
        return aggregate_samples(samples)

    @staticmethod
    def _dataset_root(root: Path) -> Path:
        candidates = [
            root,
            root / "data" / DATASET_DIR_NAME,
        ]
        data_root = root / "data"
        if data_root.is_dir():
            candidates.extend(
                path for path in sorted(data_root.iterdir()) if path.is_dir()
            )
        for candidate in candidates:
            if (candidate / "tasks").is_dir() and (candidate / "assets").is_dir():
                return candidate.resolve()
        raise BenchFormatError(
            f"{root}: expected tasks/ and assets/, or data/{DATASET_DIR_NAME}/"
        )
