"""SWE-Bench Pro adapter, Harness image integration, and official scorer."""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import replace
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

_IMAGE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]*$")
_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_BUILD_LOCK = threading.Lock()
_DERIVED_IMAGES: dict[tuple[str, str, str], str] = {}


_GRADER_SOURCE = r"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

_BASELINE_REF = "refs/roller/baseline"
_GRADE_DIR = ".roller-grade"
_OUTPUT_DIR = ".roller_outputs"


def _run(command, workspace, timeout=None):
    return subprocess.run(
        command,
        cwd=workspace,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )


def _detail(result):
    return (result.stderr or result.stdout or "").strip()[-4000:]


def _safe_relative(value):
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("unsafe test path: %r" % value)
    return path


def _remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        os.symlink(os.readlink(source), destination)
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    elif source.is_file():
        shutil.copy2(source, destination, follow_symlinks=False)


def _strip_binary_hunks(patch):
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    kept = []
    for section in sections:
        if not section.strip():
            continue
        if re.search(r"^Binary files .* differ$", section, re.MULTILINE):
            continue
        if re.search(r"^GIT binary patch$", section, re.MULTILINE):
            continue
        kept.append(section)
    return "".join(kept)


def _error(message, **details):
    payload = {"overall_score": 0.0, "resolved": 0, "error": message}
    payload.update(details)
    return payload


def roller_grade(
    workspace_path,
    fail_to_pass,
    pass_to_pass,
    selected_test_files,
    test_files,
    test_timeout_seconds=3300,
):
    workspace = Path(workspace_path)
    grade_dir = workspace / _GRADE_DIR
    output_dir = workspace / _OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    model_patch_path = output_dir / "model.patch"
    stdout_path = output_dir / "test_stdout.log"
    stderr_path = output_dir / "test_stderr.log"
    parsed_path = output_dir / "test_output.json"
    test_patch_path = grade_dir / "test.patch"
    run_script_path = grade_dir / "run_script.sh"
    parser_path = grade_dir / "parser.py"

    try:
        for path in (test_patch_path, run_script_path, parser_path):
            if not path.is_file():
                return _error("missing staged grader file: %s" % path.name)

        intent = _run(["git", "add", "-N", "--", "."], workspace)
        if intent.returncode != 0:
            return _error("could not enumerate model changes: %s" % _detail(intent))
        diff = _run(
            [
                "git",
                "diff",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                _BASELINE_REF,
                "--",
                ".",
            ],
            workspace,
        )
        if diff.returncode != 0:
            return _error("could not extract model patch: %s" % _detail(diff))
        model_patch = _strip_binary_hunks(diff.stdout)
        model_patch_path.write_text(model_patch, encoding="utf-8")

        reset = _run(["git", "reset", "--hard", _BASELINE_REF], workspace)
        if reset.returncode != 0:
            return _error("could not restore benchmark baseline: %s" % _detail(reset))
        clean = _run(["git", "clean", "-fd"], workspace)
        if clean.returncode != 0:
            return _error("could not clean benchmark baseline: %s" % _detail(clean))

        hidden = _run(["git", "apply", "--verbose", str(test_patch_path)], workspace)
        if hidden.returncode != 0:
            return _error("hidden test patch did not apply: %s" % _detail(hidden))
        hidden_intent = _run(["git", "add", "-N", "--", "."], workspace)
        if hidden_intent.returncode != 0:
            return _error("could not enumerate hidden tests: %s" % _detail(hidden_intent))
        changed = _run(
            [
                "git",
                "-c",
                "core.quotepath=false",
                "diff",
                "--name-only",
                "-z",
                _BASELINE_REF,
                "--",
                ".",
            ],
            workspace,
        )
        if changed.returncode != 0:
            return _error("could not enumerate hidden test files: %s" % _detail(changed))
        hidden_paths = list(test_files)
        hidden_paths.extend(item for item in changed.stdout.split("\0") if item)
        hidden_paths = list(dict.fromkeys(hidden_paths))

        with tempfile.TemporaryDirectory(prefix="roller-swebench-tests-") as temporary:
            snapshot = Path(temporary)
            present = set()
            for index, relative in enumerate(hidden_paths):
                relative_path = _safe_relative(relative)
                source = workspace.joinpath(*relative_path.parts)
                if source.exists() or source.is_symlink():
                    _copy(source, snapshot / str(index))
                    present.add(index)

            for relative in hidden_paths:
                relative_path = _safe_relative(relative)
                _remove(workspace.joinpath(*relative_path.parts))
            reset = _run(["git", "reset", "--hard", _BASELINE_REF], workspace)
            if reset.returncode != 0:
                return _error("could not reset after staging hidden tests: %s" % _detail(reset))
            clean = _run(["git", "clean", "-fd"], workspace)
            if clean.returncode != 0:
                return _error("could not clean after staging hidden tests: %s" % _detail(clean))

            if model_patch.strip():
                applied = _run(["git", "apply", "--verbose", str(model_patch_path)], workspace)
                if applied.returncode != 0:
                    return _error(
                        "model patch did not apply to a clean instance: %s" % _detail(applied)
                    )

            for index, relative in enumerate(hidden_paths):
                relative_path = _safe_relative(relative)
                destination = workspace.joinpath(*relative_path.parts)
                _remove(destination)
                if index in present:
                    _copy(snapshot / str(index), destination)

        selected_argument = ",".join(selected_test_files)
        with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr_handle:
            try:
                test_result = subprocess.run(
                    ["bash", str(run_script_path), selected_argument],
                    cwd=workspace,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                    timeout=test_timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                return _error(
                    "official test script timed out",
                    test_timeout_seconds=test_timeout_seconds,
                )

        parsed = _run(
            [
                sys.executable,
                str(parser_path),
                str(stdout_path),
                str(stderr_path),
                str(parsed_path),
            ],
            workspace,
            timeout=120,
        )
        if parsed.returncode != 0 or not parsed_path.is_file():
            return _error(
                "official result parser failed: %s" % _detail(parsed),
                test_returncode=test_result.returncode,
            )
        try:
            output = json.loads(parsed_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return _error(
                "official result parser produced invalid JSON: %s" % exc,
                test_returncode=test_result.returncode,
            )
        tests = output.get("tests") if isinstance(output, dict) else None
        if not isinstance(tests, list):
            return _error(
                "official result parser produced no tests list",
                test_returncode=test_result.returncode,
            )
        passed = {
            item.get("name")
            for item in tests
            if isinstance(item, dict)
            and item.get("status") == "PASSED"
            and isinstance(item.get("name"), str)
        }
        required = set(fail_to_pass) | set(pass_to_pass)
        missing = sorted(required - passed)
        resolved = not missing
        return {
            "overall_score": float(resolved),
            "resolved": int(resolved),
            "test_returncode": test_result.returncode,
            "tests_seen": len(tests),
            "required_tests": len(required),
            "passed_required_tests": len(required) - len(missing),
            "fail_to_pass_tests": len(fail_to_pass),
            "pass_to_pass_tests": len(pass_to_pass),
            "missing_tests": missing,
            "model_patch_bytes": len(model_patch.encode("utf-8")),
        }
    except Exception as exc:
        return _error("SWE-Bench Pro grader failed: %s: %s" % (type(exc).__name__, exc))
"""


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


def _setup_command(base_commit: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", base_commit):
        raise BenchFormatError(f"invalid base_commit {base_commit!r}")
    return f"""\
set -euo pipefail
git config --global --add safe.directory {WORKSPACE_CONTAINER_PATH}
git reset --hard {base_commit}
git clean -fd
git checkout {base_commit}
git ls-files -z > /tmp/roller-swebench-tracked
rm -rf .git
git init -q
git config user.name Roller
git config user.email roller@localhost
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
        from pyarrow import parquet
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


def _image_id(runtime: Any, image: str) -> str:
    inspected = runtime.run(
        ["image", "inspect", image, "--format", "{{.Id}}"], timeout=60
    )
    if inspected.returncode != 0:
        logger.info("pulling SWE-Bench Pro runtime image %s", image)
        pulled = runtime.run(["pull", image], timeout=3600)
        if pulled.returncode != 0:
            detail = (pulled.stderr or pulled.stdout or "").strip()[-2000:]
            raise RuntimeError(f"could not pull Docker image {image}: {detail}")
        inspected = runtime.run(
            ["image", "inspect", image, "--format", "{{.Id}}"], timeout=60
        )
    if inspected.returncode != 0 or not inspected.stdout.strip():
        detail = (inspected.stderr or inspected.stdout or "").strip()[-1000:]
        raise RuntimeError(f"could not inspect Docker image {image}: {detail}")
    return inspected.stdout.strip()


def ensure_harness_image(
    runtime: Any,
    source_image: str,
    harness_image: str,
    harness_name: str,
) -> str:
    """Build an Agent image with one Harness and the official repository snapshot."""

    for image in (source_image, harness_image):
        if not _IMAGE_REF_RE.fullmatch(image):
            raise ValueError(f"unsafe Docker image reference: {image!r}")
    with _BUILD_LOCK:
        source_id = _image_id(runtime, source_image)
        harness_id = _image_id(runtime, harness_image)
        key = (source_id, harness_id, harness_name)
        cached = _DERIVED_IMAGES.get(key)
        if cached is not None:
            return cached
        digest = hashlib.sha256("\0".join(key).encode()).hexdigest()[:20]
        tag = f"roller-swebench-pro-{harness_name}:{digest}"
        existing = runtime.run(
            ["image", "inspect", tag, "--format", "{{.Id}}"], timeout=60
        )
        if existing.returncode == 0:
            _DERIVED_IMAGES[key] = tag
            return tag
        if harness_name == "openclaw":
            dockerfile = f"""\
FROM {harness_image} AS harness
FROM {source_image}
USER root
COPY --from=harness /usr/local/bin/node /usr/local/bin/node
COPY --from=harness /app /opt/roller-openclaw
RUN ln -sf /opt/roller-openclaw/openclaw.mjs /usr/local/bin/openclaw \\
    && chmod 755 /opt/roller-openclaw/openclaw.mjs /usr/local/bin/node
ENV PATH="/usr/local/bin:${{PATH}}"
ENTRYPOINT []
CMD ["/bin/bash"]
"""
        else:
            dockerfile = f"""\
FROM {source_image} AS instance
FROM {harness_image}
USER root
COPY --from=instance /app /app
ENTRYPOINT []
CMD ["/bin/bash"]
"""
        with tempfile.TemporaryDirectory(prefix="roller-swebench-image-") as temporary:
            context = Path(temporary)
            (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            built = runtime.run(["build", "-t", tag, str(context)], timeout=3600)
        if built.returncode != 0:
            detail = (built.stderr or built.stdout or "").strip()[-4000:]
            raise RuntimeError(
                f"could not combine {harness_name} with SWE-Bench Pro image "
                f"{source_image}: {detail}"
            )
        _DERIVED_IMAGES[key] = tag
        return tag


def ensure_openclaw_image(runtime: Any, source_image: str, openclaw_image: str) -> str:
    """Compatibility helper retained for callers of the initial integration."""

    return ensure_harness_image(runtime, source_image, openclaw_image, "openclaw")


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
            result = self.inner.runtime.run(
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

    def _grade(self, session: Any, request: Any, status: str) -> GradeOutcome:
        started = time.monotonic()
        if not getattr(session, "container_started", False):
            return super()._grade(session, request, status)
        patch, error = self._extract_patch(session)
        if error is not None:
            return GradeOutcome(
                {"overall_score": 0.0, "resolved": 0, "error": error},
                time.monotonic() - started,
                error,
            )

        runtime = self.inner.runtime
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
                    _setup_command(base_commit),
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
            outcome = ContainerGrader(self.binding.grade).grade(
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
            runtime.run(["rm", "-f", grader_container], timeout=30)


class SWEBenchProAdapter(BenchAdapter):
    name = "swebench-pro"
    description = "SWE-Bench Pro public split with official instance-image tests"

    def __init__(self, cache_root: Path | None = None) -> None:
        self.cache_root = (
            cache_root.expanduser().resolve() if cache_root else _default_cache_root()
        )

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
                    },
                    timeout_seconds=GRADE_TIMEOUT_SECONDS,
                    needs_transcript=False,
                    workspace_path=WORKSPACE_CONTAINER_PATH,
                    stage=(
                        (str(test_patch_path), f"{GRADE_DIR_NAME}/test.patch"),
                        (str(run_script.resolve()), f"{GRADE_DIR_NAME}/run_script.sh"),
                        (str(parser.resolve()), f"{GRADE_DIR_NAME}/parser.py"),
                    ),
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
                    "runtime": {"setup": [_setup_command(base_commit)]},
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
    "ensure_openclaw_image",
]
