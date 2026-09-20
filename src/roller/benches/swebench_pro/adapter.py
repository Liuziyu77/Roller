"""SWE-Bench Pro task export and Harness integration."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from roller.benches.base import (
    BenchAdapter,
    BenchBinding,
    BenchFormatError,
    GradeSpec,
)
from roller.benches.swebench_pro.constants import (
    DEFAULT_TIMEOUT_SECONDS,
    EVALUATION_PROTOCOL_REVISION,
    GRADE_DIR_NAME,
    GRADE_TIMEOUT_SECONDS,
    OUTPUT_DIR_NAME,
    SOURCE_IMAGE_PREFIX,
    TASK_SCHEMA,
    TEST_TIMEOUT_SECONDS,
    WORKSPACE_CONTAINER_PATH,
)
from roller.benches.swebench_pro.grader_runtime import (
    SWEBenchProGradedHarness,
    _GRADER_SOURCE,
)
from roller.benches.swebench_pro.images import ensure_harness_image
from roller.benches.swebench_pro.report import aggregate
from roller.benches.swebench_pro.task_parser import (
    _clean_text,
    _dataset_path,
    _load_dataset,
    _string_list,
    _test_files,
)
from roller.benches.swebench_pro.workspace import (
    _default_cache_root,
    _materialize_test_patch,
    _setup_command,
    _test_fixture_patch,
)

_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SWEBenchProAdapter(BenchAdapter):
    name = "swebench-pro"
    description = "SWE-Bench Pro public split with official instance-image tests"

    def __init__(
        self,
        cache_root: Path | None = None,
        *,
        repair_test_fixtures: bool = False,
        exclude_agent_tests: bool = False,
        go_proxy: str | None = None,
    ) -> None:
        self.cache_root = (
            cache_root.expanduser().resolve() if cache_root else _default_cache_root()
        )
        self.repair_test_fixtures = repair_test_fixtures
        self.exclude_agent_tests = exclude_agent_tests
        self.go_proxy = go_proxy

    def load(
        self,
        root: Path,
        *,
        categories: Sequence[str] | None = None,
        task_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        root = root.expanduser().resolve()
        records = _load_dataset(_dataset_path(root))
        requested_categories = set(categories or ())
        requested_ids = set(task_ids or ())
        known_categories: set[str] = set()
        known_ids: set[str] = set()
        rows: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            label = f"SWE-Bench Pro row {index}"
            instance_id = _clean_text(record.get("instance_id"), f"{label}.instance_id")
            repo = _clean_text(record.get("repo"), f"{label}.repo")
            if not _INSTANCE_ID_RE.fullmatch(instance_id):
                raise BenchFormatError(f"{label}: unsafe instance_id {instance_id!r}")
            if instance_id in known_ids:
                raise BenchFormatError(
                    f"{label}: duplicate instance_id {instance_id!r}"
                )
            known_ids.add(instance_id)
            known_categories.add(repo)
            if requested_categories and repo not in requested_categories:
                continue
            if requested_ids and instance_id not in requested_ids:
                continue

            base_commit = _clean_text(
                record.get("base_commit"), f"{instance_id}.base_commit"
            )
            dockerhub_tag = _clean_text(
                record.get("dockerhub_tag"), f"{instance_id}.dockerhub_tag"
            )
            test_patch = record.get("test_patch")
            if not isinstance(test_patch, str) or not test_patch.strip():
                raise BenchFormatError(f"{instance_id}: test_patch is empty")
            scripts = root / "run_scripts" / instance_id
            run_script = scripts / "run_script.sh"
            parser = scripts / "parser.py"
            for path in (run_script, parser):
                if not path.is_file():
                    raise BenchFormatError(
                        f"{instance_id}: missing official grader file {path}"
                    )
            test_patch_path = _materialize_test_patch(
                root, self.cache_root, instance_id, test_patch
            )
            fixture_paths: list[str] = []
            fixture_stage: tuple[tuple[str, str], ...] = ()
            if self.repair_test_fixtures:
                fixture_patch, fixture_paths = _test_fixture_patch(
                    record.get("patch") or ""
                )
                if fixture_paths:
                    fixture_path = _materialize_test_patch(
                        root, self.cache_root, f"{instance_id}.fixtures", fixture_patch
                    )
                    fixture_stage = (
                        (str(fixture_path), f"{GRADE_DIR_NAME}/test-fixtures.patch"),
                    )
            fail_to_pass = _string_list(
                record.get("fail_to_pass"), f"{instance_id}.fail_to_pass"
            )
            pass_to_pass = _string_list(
                record.get("pass_to_pass"), f"{instance_id}.pass_to_pass"
            )
            selected = _string_list(
                record.get("selected_test_files_to_run"),
                f"{instance_id}.selected_test_files_to_run",
            )
            restore_files = _test_files(
                record.get("before_repo_set_cmd"),
                f"{instance_id}.before_repo_set_cmd",
            )
            source_image = f"{SOURCE_IMAGE_PREFIX}{dockerhub_tag}"
            prompt = (
                f"{_clean_text(record.get('problem_statement'), f'{instance_id}.problem_statement')}"
                "\n\nRequirements:\n"
                f"{_clean_text(record.get('requirements'), f'{instance_id}.requirements')}"
                "\n\nNew interfaces introduced:\n"
                f"{_clean_text(record.get('interface'), f'{instance_id}.interface')}"
            )
            binding = BenchBinding(
                name=self.name,
                category=repo,
                metadata={
                    "upstream_task_id": instance_id,
                    "repo": repo,
                    "repo_language": _clean_text(
                        record.get("repo_language"), f"{instance_id}.repo_language"
                    ),
                    "base_commit": base_commit,
                    "source_image": source_image,
                    "go_proxy": self.go_proxy,
                    "evaluation_protocol": {
                        "revision": EVALUATION_PROTOCOL_REVISION,
                        "repair_test_fixtures": self.repair_test_fixtures,
                        "exclude_agent_tests": self.exclude_agent_tests,
                    },
                    "issue_specificity": _string_list(
                        record.get("issue_specificity"),
                        f"{instance_id}.issue_specificity",
                    ),
                    "issue_categories": _string_list(
                        record.get("issue_categories"),
                        f"{instance_id}.issue_categories",
                    ),
                },
                grade=GradeSpec(
                    code=_GRADER_SOURCE,
                    entrypoint="roller_grade",
                    kwargs={
                        "fail_to_pass": fail_to_pass,
                        "pass_to_pass": pass_to_pass,
                        "selected_test_files": selected,
                        "test_files": restore_files,
                        "test_timeout_seconds": TEST_TIMEOUT_SECONDS,
                        "exclude_agent_tests": self.exclude_agent_tests,
                        "supplemental_test_fixtures": fixture_paths,
                        "prepare_go_dependencies": bool(self.go_proxy),
                    },
                    timeout_seconds=GRADE_TIMEOUT_SECONDS,
                    needs_transcript=False,
                    workspace_path=WORKSPACE_CONTAINER_PATH,
                    stage=(
                        (str(test_patch_path), f"{GRADE_DIR_NAME}/test.patch"),
                        (str(run_script.resolve()), f"{GRADE_DIR_NAME}/run_script.sh"),
                        (str(parser.resolve()), f"{GRADE_DIR_NAME}/parser.py"),
                    )
                    + fixture_stage,
                ),
            )
            rows.append(
                {
                    "schema_version": TASK_SCHEMA,
                    "id": instance_id,
                    "harness": "openclaw",
                    "prompt": prompt,
                    "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
                    "files": {"output_dir": OUTPUT_DIR_NAME},
                    "runtime": {
                        "setup": [_setup_command(base_commit, go_proxy=self.go_proxy)]
                    },
                    "bench": binding.as_dict(),
                }
            )

        unknown_categories = requested_categories - known_categories
        if unknown_categories:
            names = ", ".join(sorted(unknown_categories))
            raise BenchFormatError(f"unknown SWE-Bench Pro categories: {names}")
        unknown_ids = requested_ids - known_ids
        if unknown_ids:
            names = ", ".join(sorted(unknown_ids))
            raise BenchFormatError(f"unknown SWE-Bench Pro task ids: {names}")
        return rows

    def wrap_harness(self, harness: Any, binding: BenchBinding) -> Any:
        source_image = binding.metadata.get("source_image")
        if not isinstance(source_image, str) or not source_image:
            raise BenchFormatError(
                "SWE-Bench Pro task is missing metadata.source_image"
            )
        image = ensure_harness_image(
            harness.runtime,
            source_image,
            harness.config.docker_image,
            harness.name,
        )
        config = replace(
            harness.config,
            docker_image=image,
            workspace_container_path=WORKSPACE_CONTAINER_PATH,
            container_user="0",
        )
        prepared = type(harness)(
            config,
            harness.worker_slot,
            runtime=harness.runtime,
        )
        return SWEBenchProGradedHarness(prepared, binding)

    def aggregate(self, samples: Iterable[dict[str, Any]]) -> dict[str, Any]:
        return aggregate(samples)
