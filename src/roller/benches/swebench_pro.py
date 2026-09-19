"""SWE-Bench Pro adapter, Harness image integration, and official scorer."""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import replace
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any

from roller.benches.base import (
    BenchAdapter,
    BenchBinding,
    BenchFormatError,
    GradeSpec,
)
from roller.benches.grading import ContainerGrader, GradedHarness, GradeOutcome
from roller.benches.report import mean_aggregate
from roller.benches.swebench_pro_images import (
    _DERIVED_IMAGES,
    _image_id,
    ensure_harness_image,
)

logger = logging.getLogger(__name__)

TASK_SCHEMA = "roller.task/v1"
WORKSPACE_CONTAINER_PATH = "/app"
OUTPUT_DIR_NAME = ".roller_outputs"
GRADE_DIR_NAME = ".roller-grade"
DEFAULT_TIMEOUT_SECONDS = 3600
GRADE_TIMEOUT_SECONDS = 3600
TEST_TIMEOUT_SECONDS = 3300
SOURCE_IMAGE_PREFIX = "jefzda/sweap-images:"
BASELINE_REF = "refs/roller/baseline"
EVALUATION_PROTOCOL_REVISION = "2026-09-19-v2"

_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


_GRADER_SOURCE = "\n" + resources.files("roller.benches").joinpath(
    "swebench_pro_grader.py"
).read_text(encoding="utf-8")


def _clean_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise BenchFormatError(f"{label}: must be a string")
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, str):
            text = decoded.strip()
    return text


def _string_list(value: Any, label: str) -> list[str]:
    if isinstance(value, list):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise BenchFormatError(f"{label}: invalid list literal") from exc
    else:
        raise BenchFormatError(f"{label}: must be a list or list literal")
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) for item in parsed
    ):
        raise BenchFormatError(f"{label}: must contain only strings")
    return parsed


def _test_files(value: Any, label: str) -> list[str]:
    command = _clean_text(value, label)
    lines = [line.strip() for line in command.splitlines() if line.strip()]
    if not lines:
        raise BenchFormatError(f"{label}: command is empty")
    try:
        tokens = shlex.split(lines[-1])
        separator = tokens.index("--")
    except (ValueError, IndexError) as exc:
        raise BenchFormatError(
            f"{label}: final command must be 'git checkout <commit> -- <paths>'"
        ) from exc
    if tokens[:2] != ["git", "checkout"] or separator < 3:
        raise BenchFormatError(
            f"{label}: final command must be 'git checkout <commit> -- <paths>'"
        )
    paths = tokens[separator + 1 :]
    if not paths:
        raise BenchFormatError(f"{label}: final checkout has no test paths")
    for path in paths:
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or not parsed.parts or ".." in parsed.parts:
            raise BenchFormatError(f"{label}: unsafe test path {path!r}")
    return paths


def _setup_command(base_commit: str, *, go_proxy: str | None = None) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", base_commit):
        raise BenchFormatError(f"invalid base_commit {base_commit!r}")
    command = f"""\
set -euo pipefail
git config --global --add safe.directory {WORKSPACE_CONTAINER_PATH}
git reset --hard {base_commit}
git clean -fd
git checkout {base_commit}
git ls-files -z > /tmp/roller-swebench-tracked
rm -rf .git
git init -q
git config user.name zqy
git config user.email zqy@localhost
cat > .git/info/exclude <<'EOF'
{OUTPUT_DIR_NAME}/
{GRADE_DIR_NAME}/
AGENTS.md
BOOTSTRAP.md
HEARTBEAT.md
IDENTITY.md
MEMORY.md
openclaw-workspace-state.json
SOUL.md
TOOLS.md
USER.md
EOF
xargs -0 -r git add -f -- < /tmp/roller-swebench-tracked
git commit -qm roller-baseline
git update-ref {BASELINE_REF} HEAD
rm -f /tmp/roller-swebench-tracked
mkdir -p {OUTPUT_DIR_NAME}
"""
    if go_proxy:
        command += f"""\
if ! command -v go >/dev/null 2>&1 && [ -x /usr/local/go/bin/go ]; then
    ln -sf /usr/local/go/bin/go /usr/local/bin/go
    if [ -x /usr/local/go/bin/gofmt ] && ! command -v gofmt >/dev/null 2>&1; then
        ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt
    fi
fi
if command -v go >/dev/null 2>&1; then
    go env -w GOPATH="$(go env GOPATH)" GOMODCACHE="$(go env GOMODCACHE)" GOPROXY={shlex.quote(go_proxy)}
fi
"""
    return command


def _dataset_path(root: Path) -> Path:
    candidates = (
        root / "test.jsonl",
        root / "data.jsonl",
        root / "test.parquet",
        root / "data" / "test.parquet",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    names = ", ".join(str(path.relative_to(root)) for path in candidates)
    raise BenchFormatError(
        f"SWE-Bench Pro data not found under {root}; expected {names}"
    )


def _load_dataset(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as stream:
            for line_number, raw in enumerate(stream, start=1):
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise BenchFormatError(
                        f"{path}:{line_number}: invalid JSON: {exc.msg}"
                    ) from exc
                if not isinstance(row, dict):
                    raise BenchFormatError(
                        f"{path}:{line_number}: row must be an object"
                    )
                rows.append(row)
        return rows
    try:
        parquet = importlib.import_module("pyarrow.parquet")
    except ImportError as exc:
        raise BenchFormatError(
            "reading SWE-Bench Pro parquet requires `pip install -e "
            '".[swebench-pro]"`, or provide test.jsonl'
        ) from exc
    return parquet.read_table(path).to_pylist()


def _default_cache_root() -> Path:
    configured = os.environ.get("SWEBENCH_PRO_CACHE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return (base / "roller" / "benches" / "swebench-pro").resolve()


def _materialize_test_patch(
    root: Path,
    cache_root: Path,
    instance_id: str,
    content: str,
) -> Path:
    packaged = root / "test_patches" / f"{instance_id}.patch"
    if packaged.is_file():
        if packaged.read_text(encoding="utf-8") != content:
            raise BenchFormatError(
                f"{instance_id}: packaged test patch does not match data"
            )
        return packaged.resolve()
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    destination = cache_root / "test_patches" / f"{digest}.patch"
    if destination.is_file():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{digest}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary_name, destination)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    return destination



def _test_fixture_patch(content: str) -> tuple[str, list[str]]:
    """Extract non-executable test data that upstream omitted from test_patch."""
    kept: list[str] = []
    paths: list[str] = []
    directories = {"testdata", "test_data", "fixtures", "__fixtures__"}
    suffixes = {".json", ".yaml", ".yml", ".txt", ".csv", ".xml", ".toml"}
    for section in re.split(r"(?=^diff --git )", content, flags=re.MULTILINE):
        if not section.startswith("diff --git "):
            continue
        try:
            header = shlex.split(section.splitlines()[0])
        except ValueError:
            continue
        if len(header) != 4 or header[2][2:] != header[3][2:]:
            continue
        path = PurePosixPath(header[3][2:])
        if (
            path.is_absolute()
            or ".." in path.parts
            or not directories.intersection(path.parts[:-1])
            or path.suffix.lower() not in suffixes
            or re.search(
                r"^(?:new file mode|new mode|old mode) 120000$",
                section,
                re.MULTILINE,
            )
            or "GIT binary patch" in section
        ):
            continue
        kept.append(section)
        paths.append(str(path))
    return "".join(kept), paths


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
                    ) + fixture_stage,
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
        return mean_aggregate(samples)


__all__ = [
    "SWEBenchProAdapter",
    "SWEBenchProGradedHarness",
    "ensure_harness_image",
]
