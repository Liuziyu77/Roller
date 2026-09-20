"""Self-contained QwenClawBench grader generation and harness integration."""

from __future__ import annotations

import ast
import json
import re
import tempfile

from roller.benches.grading import (
    GradedHarness,
    GradeOutcome,
    workspace_container_path,
)
from roller.benches.qwenclaw.task_parser import QwenClawTask
from roller.benches.qwenclaw.workspace import workspace_compatibility_script
from roller.benches.transcripts import grader_messages, session_trajectory

IMPORT_LINE_RE = re.compile(
    r"^(?P<indent>[ \t]*)import\s+(?P<names>[A-Za-z0-9_., \t]+)$",
    re.MULTILINE,
)

WORKSPACE_CONTAINER_PATH = "/home/node/workspace"
OUTPUT_DIR_NAME = ".roller_outputs"
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

def _roller_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ("text", "thinking", "reasoning"):
            value = content.get(key)
            if isinstance(value, str):
                return value
        return ""
    if isinstance(content, list):
        return "\n".join(filter(None, (_roller_text(item) for item in content)))
    return ""

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
                item_type = item.get("type") if isinstance(item, dict) else None
                if isinstance(item, str) or item_type == "text":
                    text = _roller_text(item)
                    if text:
                        parts.append("Assistant: %s" % text[:2000])
                elif item_type in ("thinking", "reasoning"):
                    text = _roller_text(item)
                    if text:
                        parts.append("Reasoning: %s" % text[:2000])
                elif isinstance(item, dict) and item.get("type") in ("toolCall", "tool_use"):
                    arguments = item.get("arguments", item.get("input", {}))
                    parts.append("Tool: %s(%s)" % (
                        item.get("name"),
                        _roller_json.dumps(arguments, ensure_ascii=False),
                    ))
        elif role == "toolResult":
            text = _roller_text(content)
            if text:
                parts.append("Result: %s" % text[:2000])
        elif role == "user":
            text = _roller_text(content)
            if text:
                parts.append("User: %s" % text[:2000])
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


def _remove_unused_yaml_import(code: str) -> str:
    tree = ast.parse(code)
    if any(
        isinstance(node, ast.Name)
        and node.id == "yaml"
        and isinstance(node.ctx, ast.Load)
        for node in ast.walk(tree)
    ):
        return code

    def remove_yaml(match: re.Match[str]) -> str:
        names = [name.strip() for name in match.group("names").split(",")]
        if "yaml" not in names:
            return match.group(0)
        names.remove("yaml")
        if not names:
            return ""
        return f"{match.group('indent')}import {', '.join(names)}"

    return IMPORT_LINE_RE.sub(remove_yaml, code)


def build_grader_source(task: QwenClawTask) -> str:
    automated = _remove_unused_yaml_import(task.automated_code)
    runtime = (
        _GRADER_RUNTIME.replace("__ROLLER_GRADING_TYPE__", repr(task.grading_type))
        .replace("__ROLLER_PROMPT__", repr(task.prompt))
        .replace("__ROLLER_EXPECTED__", repr(task.expected_behavior))
        .replace("__ROLLER_RUBRIC__", repr(task.llm_judge_rubric))
        .replace("__ROLLER_WEIGHTS__", repr(task.grading_weights))
        .replace("__ROLLER_OUTPUT_DIR__", repr(OUTPUT_DIR_NAME))
    )
    return (automated + "\n\n" if automated else "") + runtime.strip() + "\n"


def _session_trajectory(session):
    # Preserve the old module-level monkeypatch point after moving this class
    # into a submodule.
    from roller.benches import qwenclaw

    return qwenclaw.session_trajectory(session)


class QwenClawGradedHarness(GradedHarness):
    """Normalize each native journal before the upstream grader sees it."""

    def _prepare_session(self, session) -> None:
        super()._prepare_session(session)
        workspace = workspace_container_path(session)
        result = self.inner.runtime.run(
            [
                "exec",
                session.container,
                "/bin/sh",
                "-c",
                workspace_compatibility_script(workspace),
            ],
            timeout=30,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-500:]
            raise RuntimeError(
                "QwenClawBench workspace compatibility setup failed"
                + (f": {detail}" if detail else "")
            )

    def _grade(self, session, request, status):
        if not getattr(session, "container_started", False):
            return super()._grade(session, request, status)
        try:
            events = grader_messages(_session_trajectory(session))
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
