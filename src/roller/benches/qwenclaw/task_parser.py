"""QwenClawBench Markdown task parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from roller.benches.base import BenchFormatError

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
SECTION_RE = re.compile(r"^##\s+(.+)$")
PYTHON_FENCE_RE = re.compile(r"```python\s*\n(.*?)\n\s*```", re.DOTALL)
TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
DEFAULT_TIMEOUT_SECONDS = 1800


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
    task_id = task_id.strip()
    if not TASK_ID_RE.fullmatch(task_id):
        raise BenchFormatError(f"{label}: unsafe task id {task_id!r}")
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
        task_id=task_id,
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
