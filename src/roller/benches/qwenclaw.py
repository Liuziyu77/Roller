"""QwenClawBench adapter and self-contained hybrid grader."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from roller.benches.base import BenchAdapter, BenchBinding, BenchFormatError, GradeSpec
from roller.benches.grading import GradedHarness, GradeOutcome
from roller.benches.report import mean_aggregate, sample_score
from roller.benches.transcripts import grader_messages, session_trajectory

logger = logging.getLogger(__name__)

TASK_SCHEMA = "roller.task/v1"
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
SECTION_RE = re.compile(r"^##\s+(.+)$")
PYTHON_FENCE_RE = re.compile(r"```python\s*\n(.*?)\n\s*```", re.DOTALL)

DATASET_DIR_NAME = "qwenclawbench-v1.1-100"
WORKSPACE_CONTAINER_PATH = "/home/node/workspace"
OUTPUT_DIR_NAME = ".roller_outputs"
DEFAULT_TIMEOUT_SECONDS = 1800
GRADE_TIMEOUT_SECONDS = 1800
AUTO_PENALTY_THRESHOLD = 0.75

JUDGE_ENV_NAMES = (
    "QWENCLAW_JUDGE_BASE_URL",
    "QWENCLAW_JUDGE_API_KEY",
    "QWENCLAW_JUDGE_MODEL",
    "QWENCLAW_JUDGE_MAX_RETRIES",
    "JUDGE_BASE_URL",
    "JUDGE_API_KEY",
    "JUDGE_MODEL",
)


@dataclass(frozen=True)
class QwenClawTask:
    task_id: str
    name: str
    category: str
    subcategory: str
    grading_type: str
    grading_weights: dict[str, float]
    timeout_seconds: int
    input_modality: str
    external_dependency: str
    workspace_files: tuple[dict[str, Any], ...]
    prompt: str
    expected_behavior: str
    grading_criteria: tuple[str, ...]
    automated_code: str
    llm_judge_rubric: str
    source_path: Path


def split_sections(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in body.splitlines():
        match = SECTION_RE.match(line)
        if match:
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current = match.group(1).strip()
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()
    return sections


def _grading_criteria(raw: str) -> tuple[str, ...]:
    criteria: list[str] = []
    for line in raw.splitlines():
        match = re.match(r"^-\s+\[[ xX]\]\s+(.+)$", line.strip())
        if match:
            criteria.append(match.group(1).strip())
    return tuple(criteria)


def _python_code(raw: str, label: str) -> str:
    match = PYTHON_FENCE_RE.search(raw)
    if not match:
        raise BenchFormatError(f"{label}: missing fenced Python grader")
    code = match.group(1).strip()
    if not code:
        raise BenchFormatError(f"{label}: automated grader is empty")
    try:
        compile(code, label, "exec")
    except SyntaxError as exc:
        raise BenchFormatError(f"{label}: invalid automated grader: {exc}") from exc
    return code


def parse_task_markdown(
    text: str, label: str, source_path: Path | None = None
) -> QwenClawTask:
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise BenchFormatError(f"{label}: missing YAML frontmatter")
    frontmatter = re.sub(
        r"^source_query:\s*.*?(?=\n\w+:\s|\Z)",
        "",
        match.group(1),
        flags=re.MULTILINE | re.DOTALL,
    )
    frontmatter = re.sub(
        r"^source_line_number:.*$", "", frontmatter, flags=re.MULTILINE
    )
    try:
        metadata = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:
        raise BenchFormatError(f"{label}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(metadata, dict):
        raise BenchFormatError(f"{label}: frontmatter must be an object")
    sections = split_sections(match.group(2))

    task_id = metadata.get("id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise BenchFormatError(f"{label}: id must be a non-empty string")
    prompt = sections.get("Prompt", "").strip()
    if not prompt:
        raise BenchFormatError(f"{label}: missing ## Prompt")
    grading_type = metadata.get("grading_type", "automated")
    if grading_type not in {"automated", "llm_judge", "hybrid"}:
        raise BenchFormatError(f"{label}: unsupported grading_type {grading_type!r}")
    raw_weights = metadata.get("grading_weights") or {
        "automated": 0.5,
        "llm_judge": 0.5,
    }
    if not isinstance(raw_weights, dict):
        raise BenchFormatError(f"{label}: grading_weights must be an object")
    try:
        weights = {
            "automated": float(raw_weights.get("automated", 0.5)),
            "llm_judge": float(raw_weights.get("llm_judge", 0.5)),
        }
    except (TypeError, ValueError) as exc:
        raise BenchFormatError(f"{label}: grading weights must be numeric") from exc
    if min(weights.values()) < 0 or sum(weights.values()) <= 0:
        raise BenchFormatError(
            f"{label}: grading weights must be non-negative and non-zero"
        )

    raw_timeout = metadata.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if isinstance(raw_timeout, bool):
        raise BenchFormatError(f"{label}: timeout_seconds must be an integer")
    try:
        timeout_seconds = int(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise BenchFormatError(f"{label}: timeout_seconds must be an integer") from exc
    if timeout_seconds <= 0:
        raise BenchFormatError(f"{label}: timeout_seconds must be positive")

    workspace_files = metadata.get("workspace_files", [])
    if not isinstance(workspace_files, list) or not all(
        isinstance(item, dict) for item in workspace_files
    ):
        raise BenchFormatError(f"{label}: workspace_files must be a list of objects")
    automated_code = ""
    if grading_type in {"automated", "hybrid"}:
        automated_code = _python_code(
            sections.get("Automated Checks", ""), f"{label} ## Automated Checks"
        )
    rubric = sections.get("LLM Judge Rubric", "").strip()
    if grading_type in {"llm_judge", "hybrid"} and not rubric:
        rubric = "\n".join(
            f"- {item}"
            for item in _grading_criteria(sections.get("Grading Criteria", ""))
        )
    if grading_type in {"llm_judge", "hybrid"} and not rubric:
        raise BenchFormatError(f"{label}: LLM judge task has no rubric")

    return QwenClawTask(
        task_id=task_id.strip(),
        name=str(metadata.get("name") or "").strip(),
        category=str(metadata.get("category") or "").strip(),
        subcategory=str(metadata.get("subcategory") or "").strip(),
        grading_type=grading_type,
        grading_weights=weights,
        timeout_seconds=timeout_seconds,
        input_modality=str(metadata.get("input_modality") or "").strip(),
        external_dependency=str(metadata.get("external_dependency") or "").strip(),
        workspace_files=tuple(workspace_files),
        prompt=prompt,
        expected_behavior=sections.get("Expected Behavior", "").strip(),
        grading_criteria=_grading_criteria(sections.get("Grading Criteria", "")),
        automated_code=automated_code,
        llm_judge_rubric=rubric,
        source_path=source_path or Path(label),
    )


def _relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BenchFormatError(f"{label}: must be a non-empty relative path")
    raw = value.strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise BenchFormatError(f"{label}: unsafe path {value!r}")
    return path.as_posix()


def _source_path(assets_root: Path, task_id: str, source: str) -> Path:
    relative = _relative_path(source, f"{task_id}.workspace_files.source")
    candidates = (assets_root / task_id / relative, assets_root / relative)
    for candidate in candidates:
        if not candidate.exists():
            continue
        cursor = assets_root
        for part in candidate.relative_to(assets_root).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise BenchFormatError(
                    f"{task_id}: workspace source is a symlink: {cursor}"
                )
        resolved = candidate.resolve()
        if not resolved.is_relative_to(assets_root.resolve()):
            raise BenchFormatError(
                f"{task_id}: workspace source escapes assets: {source}"
            )
        return candidate
    searched = ", ".join(str(path) for path in candidates)
    raise BenchFormatError(
        f"{task_id}: missing workspace source {source!r}; searched {searched}"
    )


def _hash_source(digest: Any, source: Path, logical: str) -> None:
    if source.is_dir():
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise BenchFormatError(f"workspace source contains a symlink: {path}")
            relative = path.relative_to(source).as_posix()
            mode = path.stat().st_mode
            if path.is_dir():
                digest.update(f"D:{logical}/{relative}\0".encode())
            elif path.is_file() and stat.S_ISREG(mode):
                digest.update(f"F:{logical}/{relative}\0".encode())
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            else:
                raise BenchFormatError(
                    f"workspace source is not a regular file: {path}"
                )
        return
    mode = source.stat().st_mode
    if not stat.S_ISREG(mode):
        raise BenchFormatError(f"workspace source is not a regular file: {source}")
    digest.update(f"F:{logical}\0".encode())
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)


def _cache_root() -> Path:
    configured = (os.environ.get("QWENCLAW_CACHE_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    xdg = (os.environ.get("XDG_CACHE_HOME") or "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return (base / "roller" / "benches" / "qwenclawbench").resolve()


def materialize_workspace(
    task: QwenClawTask, assets_root: Path, cache_root: Path | None = None
) -> Path:
    """Build the exact source-to-destination workspace expected upstream."""

    entries: list[tuple[str, Path | None, str | None]] = []
    destinations: list[str] = []
    digest = hashlib.sha256()
    digest.update(task.source_path.read_bytes())
    for index, item in enumerate(task.workspace_files):
        destination_value = item.get("dest", item.get("path"))
        destination = _relative_path(
            destination_value, f"{task.task_id}.workspace_files[{index}].dest"
        )
        for existing in destinations:
            if (
                destination == existing
                or destination.startswith(existing + "/")
                or existing.startswith(destination + "/")
            ):
                raise BenchFormatError(
                    f"{task.task_id}: colliding workspace destinations "
                    f"{existing!r} and {destination!r}"
                )
        destinations.append(destination)
        if "content" in item:
            content = item["content"]
            if not isinstance(content, str):
                raise BenchFormatError(
                    f"{task.task_id}.workspace_files[{index}].content must be a string"
                )
            digest.update(f"C:{destination}\0{content}".encode())
            entries.append((destination, None, content))
            continue
        source_value = item.get("source")
        if not isinstance(source_value, str):
            raise BenchFormatError(
                f"{task.task_id}.workspace_files[{index}].source must be a string"
            )
        source = _source_path(assets_root, task.task_id, source_value)
        digest.update(f"M:{source_value}->{destination}\0".encode())
        _hash_source(digest, source, destination)
        entries.append((destination, source, None))

    fingerprint = digest.hexdigest()
    task_cache = (cache_root or _cache_root()) / task.task_id
    target = task_cache / fingerprint
    workspace = target / "workspace"
    if (target / "manifest.json").is_file() and workspace.is_dir():
        return workspace

    task_cache.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{fingerprint}.", dir=task_cache))
    temporary_workspace = temporary / "workspace"
    temporary_workspace.mkdir()
    try:
        for destination, source, content in entries:
            output = temporary_workspace / destination
            output.parent.mkdir(parents=True, exist_ok=True)
            if content is not None:
                output.write_text(content, encoding="utf-8")
            elif source is not None and source.is_dir():
                shutil.copytree(source, output, copy_function=shutil.copy2)
            elif source is not None:
                shutil.copy2(source, output)
        (temporary / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "roller.qwenclaw_workspace/v1",
                    "task_id": task.task_id,
                    "fingerprint": fingerprint,
                    "source": str(task.source_path),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            temporary.rename(target)
        except FileExistsError:
            shutil.rmtree(temporary)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return workspace


_GRADER_RUNTIME = r"""
import json as _roller_json
import os as _roller_os
import re as _roller_re
import shutil as _roller_shutil
import time as _roller_time
import urllib.error as _roller_urlerror
import urllib.request as _roller_urlrequest
from pathlib import Path as _RollerPath

_roller_automated_grade = globals().get("grade")
_ROLLER_GRADING_TYPE = __ROLLER_GRADING_TYPE__
_ROLLER_PROMPT = __ROLLER_PROMPT__
_ROLLER_EXPECTED = __ROLLER_EXPECTED__
_ROLLER_RUBRIC = __ROLLER_RUBRIC__
_ROLLER_WEIGHTS = __ROLLER_WEIGHTS__
_ROLLER_OUTPUT_DIR = __ROLLER_OUTPUT_DIR__

def _roller_numeric(scores):
    result = {}
    for key, value in (scores or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[str(key)] = float(value)
    return result

def _roller_average(scores):
    values = list(_roller_numeric(scores).values())
    return sum(values) / len(values) if values else 0.0

def _roller_summary(transcript):
    parts = []
    for event in transcript or []:
        if not isinstance(event, dict) or event.get("type") != "message":
            continue
        message = event.get("message") or {}
        role = message.get("role")
        content = message.get("content") or []
        if role == "assistant":
            for item in content:
                if isinstance(item, dict) and item.get("type") in ("toolCall", "tool_use"):
                    arguments = item.get("arguments", item.get("input", {}))
                    parts.append("Tool: %s(%s)" % (
                        item.get("name"),
                        _roller_json.dumps(arguments, ensure_ascii=False),
                    ))
        elif role == "toolResult" and content:
            parts.append("Result: %s" % str(content[0])[:200])
        elif role == "user" and content:
            parts.append("User: %s" % content[0])
    return "\n".join(parts)

def _roller_judge_prompt(transcript):
    return (
        "You are a grading function. Your ONLY job is to output a single JSON object.\n\n"
        "CRITICAL RULES:\n"
        "- Do NOT use any tools (no Read, Write, exec, or any other tool calls)\n"
        "- Do NOT create files or run commands\n"
        "- Do NOT write any prose, explanation, or commentary outside the JSON\n"
        "- Respond with ONLY a JSON object — nothing else\n\n"
        "Be a strict evaluator. Reserve 1.0 for genuinely excellent performance. "
        "An average acceptable completion should score around 0.6-0.7. "
        "Deduct points for unnecessary steps, verbose output, and inefficient tool usage.\n\n"
        "## Task\n%s\n\n## Expected Behavior\n%s\n\n"
        "## Agent Transcript (summarized)\n%s\n\n## Grading Rubric\n%s\n\n"
        "Score each criterion from 0.0 to 1.0.\n\n"
        "Respond with ONLY this JSON structure (no markdown, no code fences, no extra text):\n"
        '{"scores": {"criterion_name": 0.0}, "total": 0.0, "notes": "brief justification"}'
    ) % (_ROLLER_PROMPT, _ROLLER_EXPECTED, _roller_summary(transcript), _ROLLER_RUBRIC)

def _roller_json_objects(text):
    objects = []
    depth = 0
    current = []
    for char in text:
        if char == "{":
            if depth == 0:
                current = []
            depth += 1
        if depth:
            current.append(char)
        if char == "}" and depth:
            depth -= 1
            if depth == 0:
                objects.append("".join(current))
    return objects

def _roller_parse_judge(text):
    text = (text or "").strip()
    fenced = _roller_re.search(r"```(?:json)?\s*(.*?)\s*```", text, _roller_re.DOTALL)
    candidates = ([fenced.group(1)] if fenced else []) + list(reversed(_roller_json_objects(text)))
    parsed = {}
    for candidate in candidates:
        try:
            value = _roller_json.loads(candidate)
        except Exception:
            continue
        if isinstance(value, dict):
            parsed = value
            if "scores" in value:
                break
    scores = {}
    source_scores = parsed.get("scores", parsed.get("criteria_scores", {}))
    if isinstance(source_scores, dict):
        for key, value in source_scores.items():
            if isinstance(value, dict):
                value = value.get("score")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                scores[str(key)] = float(value)
    total = parsed.get("total", parsed.get("score"))
    if not isinstance(total, (int, float)) or isinstance(total, bool):
        total = _roller_average(scores)
    return scores, max(0.0, min(1.0, float(total))), str(
        parsed.get("notes", parsed.get("justification", parsed.get("reasoning", "")))
    )

def _roller_call_judge(transcript):
    base = (
        _roller_os.environ.get("QWENCLAW_JUDGE_BASE_URL")
        or _roller_os.environ.get("JUDGE_BASE_URL")
        or ""
    ).strip()
    key = (
        _roller_os.environ.get("QWENCLAW_JUDGE_API_KEY")
        or _roller_os.environ.get("JUDGE_API_KEY")
        or ""
    ).strip()
    model = (
        _roller_os.environ.get("QWENCLAW_JUDGE_MODEL")
        or _roller_os.environ.get("JUDGE_MODEL")
        or "claude-opus-4-5-20251101"
    ).strip()
    if not base or not key:
        raise RuntimeError(
            "LLM judge requires QWENCLAW_JUDGE_BASE_URL and "
            "QWENCLAW_JUDGE_API_KEY (JUDGE_BASE_URL/JUDGE_API_KEY also accepted)"
        )
    endpoint = base.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    payload = _roller_json.dumps({
        "model": model.split("/", 1)[-1] if "/" in model else model,
        "messages": [{"role": "user", "content": _roller_judge_prompt(transcript)}],
        "temperature": 0.0,
        "max_tokens": 20480,
    }).encode("utf-8")
    retries = int(_roller_os.environ.get("QWENCLAW_JUDGE_MAX_RETRIES", "3"))
    retries = max(1, min(100, retries))
    last_error = None
    for attempt in range(retries):
        request = _roller_urlrequest.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
            method="POST",
        )
        try:
            with _roller_urlrequest.urlopen(request, timeout=600) as response:
                body = _roller_json.loads(response.read().decode("utf-8"))
            choices = body.get("choices") or []
            if not choices:
                raise RuntimeError("judge API returned no choices")
            return _roller_parse_judge((choices[0].get("message") or {}).get("content", ""))
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                _roller_time.sleep(min(5 * (2 ** attempt), 30))
    raise RuntimeError("judge API failed after %d attempt(s): %s" % (retries, last_error))

def _roller_snapshot(workspace_path):
    source = _RollerPath(workspace_path)
    destination = source / _ROLLER_OUTPUT_DIR
    if destination.exists():
        _roller_shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for item in source.iterdir():
        if item.name in {_ROLLER_OUTPUT_DIR, ".git"}:
            continue
        target = destination / item.name
        if item.is_symlink():
            continue
        if item.is_dir():
            _roller_shutil.copytree(item, target, symlinks=True)
        elif item.is_file():
            _roller_shutil.copy2(item, target)

def roller_grade(transcript, workspace_path):
    try:
        automated_scores = {}
        automated_score = 0.0
        if _ROLLER_GRADING_TYPE in {"automated", "hybrid"}:
            if not callable(_roller_automated_grade):
                raise RuntimeError("automated grader function is missing")
            automated_scores = _roller_numeric(
                _roller_automated_grade(transcript, workspace_path)
            )
            automated_score = _roller_average(automated_scores)
        judge_scores = {}
        judge_score = 0.0
        notes = ""
        if _ROLLER_GRADING_TYPE in {"llm_judge", "hybrid"}:
            judge_scores, judge_score, notes = _roller_call_judge(transcript)
        if _ROLLER_GRADING_TYPE == "automated":
            overall = simple = automated_score
        elif _ROLLER_GRADING_TYPE == "llm_judge":
            overall = simple = judge_score
        else:
            auto_weight = float(_ROLLER_WEIGHTS.get("automated", 0.5))
            judge_weight = float(_ROLLER_WEIGHTS.get("llm_judge", 0.5))
            weight = auto_weight + judge_weight
            if weight <= 0:
                auto_weight = judge_weight = 0.5
                weight = 1.0
            simple = (automated_score * auto_weight + judge_score * judge_weight) / weight
            adjusted = 0.0 if automated_score < 0.75 else judge_score
            overall = (automated_score * auto_weight + adjusted * judge_weight) / weight
        return {
            "overall_score": overall,
            "score_simple": simple,
            "automated_score": automated_score,
            "llm_judge_score": judge_score,
            "automated": automated_scores,
            "llm_judge": judge_scores,
            "notes": notes,
        }
    finally:
        _roller_snapshot(workspace_path)
"""


def build_grader_source(task: QwenClawTask) -> str:
    automated = task.automated_code
    runtime = (
        _GRADER_RUNTIME.replace("__ROLLER_GRADING_TYPE__", repr(task.grading_type))
        .replace("__ROLLER_PROMPT__", repr(task.prompt))
        .replace("__ROLLER_EXPECTED__", repr(task.expected_behavior))
        .replace("__ROLLER_RUBRIC__", repr(task.llm_judge_rubric))
        .replace("__ROLLER_WEIGHTS__", repr(task.grading_weights))
        .replace("__ROLLER_OUTPUT_DIR__", repr(OUTPUT_DIR_NAME))
    )
    return (automated + "\n\n" if automated else "") + runtime.strip() + "\n"


class QwenClawGradedHarness(GradedHarness):
    """Normalize each native journal before the upstream grader sees it."""

    def _grade(self, session, request, status):
        if not getattr(session, "container_started", False):
            return super()._grade(session, request, status)
        try:
            events = grader_messages(session_trajectory(session))
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", encoding="utf-8"
            ) as handle:
                json.dump(events, handle, ensure_ascii=False)
                handle.flush()
                copied = self.inner.runtime.run(
                    [
                        "cp",
                        handle.name,
                        f"{session.container}:/tmp/roller-qwenclaw-transcript.json",
                    ],
                    timeout=60,
                )
                if copied.returncode:
                    raise RuntimeError(
                        "QwenClawBench normalized transcript staging failed"
                    )
            return super()._grade(session, request, status)
        except Exception as exc:  # noqa: BLE001 - grading failures become score metadata
            return GradeOutcome(
                {"overall_score": 0.0, "error": str(exc)}, 0.0, str(exc)
            )


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
        rows = list(samples)
        report = mean_aggregate(rows)
        grouped: dict[str, list[float]] = {}
        for row in rows:
            metadata = row.get("bench_metadata")
            upstream_id = (
                metadata.get("upstream_task_id")
                if isinstance(metadata, dict)
                else row.get("task_id")
            )
            scores = row.get("bench_scores")
            value = (
                sample_score(scores, row.get("bench_score_key") or "overall_score")
                if isinstance(scores, dict)
                else None
            )
            if isinstance(upstream_id, str) and value is not None:
                grouped.setdefault(upstream_id, []).append(value)
        task_means = {
            task_id: sum(values) / len(values) for task_id, values in grouped.items()
        }
        perfect = sum(value >= 1.0 - 1e-9 for value in task_means.values())
        report["upstream_task_count"] = len(task_means)
        report["strict_accuracy"] = (
            round(perfect / len(task_means), 6) if task_means else None
        )
        report["perfect_task_count"] = perfect
        report["runs_per_task"] = {
            task_id: len(values) for task_id, values in sorted(grouped.items())
        }
        return report

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
