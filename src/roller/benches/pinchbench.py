"""PinchBench adapter, including automated, LLM-judge and hybrid grading."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from roller.benches.base import (
    BenchAdapter,
    BenchBinding,
    BenchFormatError,
    GradeSpec,
    validate_timeout_multiplier,
)

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
PYTHON_FENCE_RE = re.compile(r"```python\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
CODE_FENCE_RE = re.compile(r"```\s*\n(.*?)\n```", re.DOTALL)
GRADING_TYPES = {"automated", "llm_judge", "hybrid"}
JUDGE_ENV = ()
JUDGE_OPTIONAL_ENV = (
    "PINCHBENCH_JUDGE_BASE_URL",
    "PINCHBENCH_JUDGE_API_KEY",
    "PINCHBENCH_JUDGE_MODEL",
    "PINCHBENCH_JUDGE_TIMEOUT_SECONDS",
    "PINCHBENCH_JUDGE_MAX_COMPLETION_TOKENS",
    "PINCHBENCH_JUDGE_MAX_TOKENS",
    "PINCHBENCH_JUDGE_MAX_TOKENS_FIELD",
    "PINCHBENCH_JUDGE_RETRIES",
    "PINCHBENCH_JUDGE_STREAM",
    "PINCHBENCH_JUDGE_NO_PROXY",
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)

_PINCH_FWS_SETUP = r'''set -euo pipefail
tools=/tmp/roller-pinch-tools
mkdir -p "$tools/bin" "$HOME/.config/gws/cache"

download_checked() {
  url=$1
  destination=$2
  checksum=$3
  if ! echo "$checksum  $destination" | sha256sum -c - >/dev/null 2>&1; then
    rm -f "$destination"
    curl --fail --location --silent --show-error \
      --retry 5 --retry-all-errors --connect-timeout 30 --max-time 600 \
      "$url" -o "$destination"
    echo "$checksum  $destination" | sha256sum -c - >/dev/null
  fi
}

if [ ! -x "$tools/node_modules/.bin/fws" ]; then
  npm install --prefix "$tools" --no-audit --no-fund @juppytt/fws@0.4.4
fi
mkdir -p "$tools/node_modules/@juppytt/fws/node_modules/.bin"
ln -sf "$tools/node_modules/.bin/tsx" \
  "$tools/node_modules/@juppytt/fws/node_modules/.bin/tsx"

download_checked \
  https://github.com/googleworkspace/cli/releases/download/v0.22.3/google-workspace-cli-x86_64-unknown-linux-gnu.tar.gz \
  /tmp/roller-pinch-gws.tar.gz \
  b951ef847b38dd41a23d31a3aeaf3cb650421aac607bbfe63727bf4f4213ce44
if [ ! -x "$tools/google-workspace-cli-x86_64-unknown-linux-gnu/gws" ]; then
  tar -xzf /tmp/roller-pinch-gws.tar.gz -C "$tools"
fi
ln -sf "$tools/google-workspace-cli-x86_64-unknown-linux-gnu/gws" \
  "$tools/bin/gws.real"

download_checked \
  https://github.com/cli/cli/releases/download/v2.79.0/gh_2.79.0_linux_amd64.tar.gz \
  /tmp/roller-pinch-gh.tar.gz \
  e7af0c72a607c0528fda1989f7c8e3be85e67d321889002af0e2938ad9c8fb68
if [ ! -x "$tools/gh_2.79.0_linux_amd64/bin/gh" ]; then
  tar -xzf /tmp/roller-pinch-gh.tar.gz -C "$tools"
fi
ln -sf "$tools/gh_2.79.0_linux_amd64/bin/gh" "$tools/bin/gh.real"

for discovery in gmail:v1 calendar:v3 drive:v3 tasks:v1 sheets:v4 people:v1; do
  api=${discovery%%:*}
  version=${discovery#*:}
  destination="$HOME/.config/gws/cache/${api}_${version}.json"
  if ! python3 -m json.tool "$destination" >/dev/null 2>&1; then
    rm -f "$destination"
    curl --fail --location --silent --show-error \
      --retry 5 --retry-all-errors --connect-timeout 30 --max-time 600 \
      "https://www.googleapis.com/discovery/v1/apis/${api}/${version}/rest" \
      -o "$destination"
    python3 -m json.tool "$destination" >/dev/null
  fi
done

export PATH="$tools/bin:$tools/node_modules/.bin:$PATH"
: "${ROLLER_GATEWAY_PORT:?Roller did not provide the OpenClaw gateway port}"
fws_port=$((ROLLER_GATEWAY_PORT + 1))
if [ "$fws_port" -gt 65534 ]; then
  fws_port=$((ROLLER_GATEWAY_PORT - 2))
fi
fws_output=$(fws server start -p "$fws_port")
printf '%s\n' "$fws_output"
printf '%s\n' "$fws_output" | sed -n '/^[[:space:]]*export /p' \
  > /roller/task/fws-environment
test -s /roller/task/fws-environment

printf '%s\n' \
  '#!/bin/bash' \
  'set -a' \
  '. /roller/task/fws-environment' \
  'set +a' \
  'exec /tmp/roller-pinch-tools/bin/gh.real "$@"' > "$tools/bin/gh"
printf '%s\n' \
  '#!/bin/bash' \
  'set -a' \
  '. /roller/task/fws-environment' \
  'set +a' \
  'exec /tmp/roller-pinch-tools/bin/gws.real "$@"' > "$tools/bin/gws"
chmod +x "$tools/bin/gh" "$tools/bin/gws"
printf '%s\n' \
  'export PATH=/tmp/roller-pinch-tools/bin:/tmp/roller-pinch-tools/node_modules/.bin:$PATH' \
  > /roller/task/environment

fws_ready=false
for attempt in $(seq 1 20); do
  if GH_REPO=testuser/my-project "$tools/bin/gh" api /user >/dev/null 2>&1; then
    fws_ready=true
    break
  fi
  sleep 0.5
done
$fws_ready || { echo 'FWS mock service failed its readiness probe' >&2; exit 1; }'''


def _pinch_runtime_setup(task: dict[str, Any]) -> list[str]:
    """Provision the mock workspace services declared by upstream PinchBench."""

    prerequisites = task["prerequisites"]
    needs_fws = task["category"].casefold() in {"gws", "github"} or any(
        "fws" in prerequisite.casefold() for prerequisite in prerequisites
    )
    return [_PINCH_FWS_SETUP] if needs_fws else []


def _split_markdown(text: str, label: str) -> tuple[dict[str, Any], dict[str, str]]:
    match = FRONTMATTER_RE.match(text)
    if match is None:
        raise BenchFormatError(f"{label}: missing YAML frontmatter")
    try:
        metadata = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise BenchFormatError(f"{label}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(metadata, dict):
        raise BenchFormatError(f"{label}: frontmatter must be an object")
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in match.group(2).splitlines():
        header = SECTION_RE.match(line)
        if header:
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current, lines = header.group(1), []
        elif current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()
    return metadata, sections


def _strip_python_fence(value: str) -> str:
    stripped = value.strip()
    match = PYTHON_FENCE_RE.search(stripped) or CODE_FENCE_RE.search(stripped)
    return match.group(1).strip() if match else stripped


def parse_task_markdown(text: str, label: str) -> dict[str, Any]:
    metadata, sections = _split_markdown(text, label)
    task_id = metadata.get("id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise BenchFormatError(f"{label}: id must be a non-empty string")
    grading_type = metadata.get("grading_type", "automated")
    if grading_type not in GRADING_TYPES:
        raise BenchFormatError(
            f"{label}: grading_type must be one of {sorted(GRADING_TYPES)}"
        )
    timeout = metadata.get("timeout_seconds", 120)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise BenchFormatError(f"{label}: timeout_seconds must be a positive integer")
    prompt = sections.get("Prompt", "").strip()
    sessions = metadata.get("sessions")
    parsed_sessions: list[dict[str, Any]] = []
    if sessions is not None:
        if not isinstance(sessions, list) or not sessions:
            raise BenchFormatError(f"{label}: sessions must be a non-empty list")
        for index, session in enumerate(sessions):
            if not isinstance(session, dict):
                raise BenchFormatError(f"{label}: sessions[{index}] must be an object")
            session_prompt = session.get("prompt")
            reset = session.get("new_session", False)
            if not isinstance(session_prompt, str) or not session_prompt.strip():
                raise BenchFormatError(
                    f"{label}: sessions[{index}].prompt must be non-empty"
                )
            if not isinstance(reset, bool) or (index == 0 and reset):
                raise BenchFormatError(
                    f"{label}: sessions[{index}].new_session must be false for the first "
                    "session and boolean elsewhere"
                )
            parsed_sessions.append(
                {
                    "role": "user",
                    "content": session_prompt.strip(),
                    "new_session": reset,
                }
            )
    elif not prompt:
        raise BenchFormatError(f"{label}: missing ## Prompt")
    workspace_files = metadata.get("workspace_files", [])
    if not isinstance(workspace_files, list):
        raise BenchFormatError(f"{label}: workspace_files must be a list")
    prerequisites = metadata.get("prerequisites", [])
    if not isinstance(prerequisites, list) or not all(
        isinstance(item, str) and item.strip() for item in prerequisites
    ):
        raise BenchFormatError(f"{label}: prerequisites must be a string list")
    return {
        "id": task_id.strip(),
        "name": str(metadata.get("name") or task_id).strip(),
        "category": str(metadata.get("category") or "").strip(),
        "grading_type": grading_type,
        "timeout_seconds": timeout,
        "workspace_files": workspace_files,
        "prompt": prompt,
        "sessions": parsed_sessions,
        "expected_behavior": sections.get("Expected Behavior", "").strip(),
        "grading_criteria": [
            match.group(1)
            for line in sections.get("Grading Criteria", "").splitlines()
            if (match := re.match(r"^-\s+\[[ x]\]\s+(.+)$", line.strip()))
        ],
        "automated_checks": _strip_python_fence(
            sections.get("Automated Checks", "")
        ),
        "llm_judge_rubric": sections.get("LLM Judge Rubric", "").strip(),
        "grading_weights": metadata.get("grading_weights") or {},
        "prerequisites": [item.strip() for item in prerequisites],
    }


_PINCH_WRAPPER = r'''
import json as _pinch_json
import os as _pinch_os
import re as _pinch_re
from pathlib import Path as _PinchPath
import time as _pinch_time
from urllib import error as _pinch_error
from urllib import request as _pinch_request

_PINCH_TASK_PROMPT = __TASK_PROMPT__
_PINCH_EXPECTED = __EXPECTED__
_PINCH_CRITERIA = __CRITERIA__
_PINCH_RUBRIC = __RUBRIC__
_PINCH_GRADING_TYPE = __GRADING_TYPE__
_PINCH_WEIGHTS = __WEIGHTS__
_PINCH_AUTOMATED = globals().get("grade")
_PINCH_OFFICIAL_JUDGE_MODEL = "anthropic/claude-haiku-4.5"
_PINCH_JUDGE_SYSTEM_MSG = (
    "You are a strict grading function. "
    "Respond with ONLY a JSON object, no prose, no markdown fences, no extra text."
)


def _pinch_average(values):
    numbers = [float(value) for value in values.values()
               if isinstance(value, (int, float)) and not isinstance(value, bool)]
    return sum(numbers) / len(numbers) if numbers else 0.0


def _pinch_normalize_transcript(transcript):
    """Present OpenClaw messages in the list-of-parts shape Pinch expects."""
    normalized = []
    for event in transcript:
        if not isinstance(event, dict):
            continue
        event = dict(event)
        message = event.get("message")
        if isinstance(message, dict):
            message = dict(message)
            content = message.get("content", [])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            elif isinstance(content, dict):
                content = [content]
            elif isinstance(content, list):
                content = [
                    item
                    if isinstance(item, dict)
                    else {"type": "text", "text": str(item)}
                    for item in content
                ]
            else:
                content = []
            message["content"] = content
            event["message"] = message
        normalized.append(event)
    return normalized


def _pinch_transcript_text(transcript):
    # Equivalent to upstream PinchBench's _summarize_transcript.
    pieces = []
    for event in transcript:
        if not isinstance(event, dict) or event.get("type") != "message":
            continue
        message = event.get("message", {})
        role = message.get("role")
        content = message.get("content", [])
        if role == "assistant":
            for item in content:
                if item.get("type") == "toolCall":
                    args = item.get("arguments", {})
                    truncated_args = {}
                    for key, value in args.items():
                        if isinstance(value, str) and len(value) > 200:
                            truncated_args[key] = value[:200] + "...[truncated]"
                        else:
                            truncated_args[key] = value
                    pieces.append(
                        f"Tool: {item.get('name')}({_pinch_json.dumps(truncated_args)})"
                    )
                elif item.get("type") == "text":
                    value = item.get("text", "").strip()
                    if value:
                        pieces.append(f"Assistant: {value[:2000]}")
        elif role == "toolResult":
            if content:
                pieces.append(f"Result: {str(content[0])[:200]}")
        elif role == "user":
            if content:
                pieces.append(f"User: {content[0]}")
    return "\n".join(pieces)


def _pinch_workspace_text(workspace_path):
    root = _PinchPath(workspace_path)
    if not root.is_dir():
        return ""
    skip_names = {
        "BOOTSTRAP.md", "SOUL.md", "USER.md", "IDENTITY.md",
        "HEARTBEAT.md", "TOOLS.md", "AGENTS.md",
    }
    skip_dirs = {".git", ".openclaw", "__pycache__", "node_modules", "skills"}
    pieces = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part.startswith(".") or part in skip_dirs for part in relative.parts):
            continue
        if path.name in skip_names:
            continue
        try:
            value = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        pieces.append(f"### File: {relative}\n{value}")
    return "\n\n".join(pieces)


def _pinch_parse_json(text):
    text = text.strip()
    if not text:
        return {}
    try:
        value = _pinch_json.loads(text)
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    fenced = _pinch_re.search(r"```(?:json)?\s*(.*?)\s*```", text, _pinch_re.S)
    if fenced:
        try:
            value = _pinch_json.loads(fenced.group(1))
            if isinstance(value, dict):
                return value
        except Exception:
            pass
    candidates = []
    depth = 0
    current = []
    for char in text:
        if char == "{":
            if depth == 0:
                current = []
            depth += 1
        if depth > 0:
            current.append(char)
        if char == "}":
            depth -= 1
            if depth == 0 and current:
                candidates.append("".join(current))
    for candidate in reversed(candidates):
        try:
            value = _pinch_json.loads(candidate)
            if isinstance(value, dict) and "scores" in value:
                return value
        except Exception:
            continue
    for candidate in reversed(candidates):
        try:
            value = _pinch_json.loads(candidate)
            if isinstance(value, dict):
                return value
        except Exception:
            continue
    score_match = _pinch_re.search(
        r"(?:total|overall|final)\s*(?:score)?[:\s]*(0\.\d+|1\.0+)",
        text,
        _pinch_re.I,
    )
    if score_match:
        total = float(score_match.group(1))
        if 0.0 <= total <= 1.0:
            return {"scores": {}, "total": total, "notes": "Score extracted from prose"}
    return {}


def _pinch_score(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    if isinstance(value, dict):
        for key in ("score", "value", "weighted_score"):
            if key in value:
                return _pinch_score(value[key])
    return None


def _pinch_named_scores(parsed):
    scores = {}
    for key in ("scores", "criteria_scores"):
        values = parsed.get(key)
        if isinstance(values, dict):
            for name, value in values.items():
                coerced = _pinch_score(value)
                if coerced is not None:
                    scores[str(name)] = coerced
    criteria = parsed.get("criterion_scores")
    if isinstance(criteria, dict):
        for name, value in criteria.items():
            coerced = _pinch_score(value)
            if coerced is not None:
                scores[str(name)] = coerced
    elif isinstance(criteria, list):
        for index, item in enumerate(criteria, start=1):
            if isinstance(item, dict):
                name = (
                    item.get("name") or item.get("criterion")
                    or item.get("label") or f"criterion_{index}"
                )
                coerced = _pinch_score(item)
            else:
                name = f"criterion_{index}"
                coerced = _pinch_score(item)
            if coerced is not None:
                scores[str(name)] = coerced
    for key, value in parsed.items():
        if _pinch_re.fullmatch(r"criterion\d+", str(key), _pinch_re.I):
            coerced = _pinch_score(value)
            if coerced is not None:
                scores[str(key)] = coerced
    return scores


def _pinch_normalize(parsed):
    scores = _pinch_named_scores(parsed)
    total = None
    for key in ("total", "score", "overall_score", "completionScore", "total_score"):
        if key in parsed:
            total = _pinch_score(parsed[key])
            if total is not None:
                break
    if total is None and isinstance(parsed.get("overall"), dict):
        total = _pinch_score(parsed["overall"])
    if total is None and scores:
        total = _pinch_average(scores)
    values = [value for value in scores.values() if isinstance(value, (int, float))]
    if (
        values and total is not None and total > 1.0
        and all(0.0 <= float(value) <= 1.0 for value in values)
    ):
        total = sum(values) / len(values)
    notes = ""
    for key in ("notes", "justification", "reasoning"):
        if key in parsed:
            notes = str(parsed[key])
            break
    return {"scores": scores, "total": total, "notes": notes}


def _pinch_env_int(name, default, minimum):
    raw = _pinch_os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


def _pinch_env_bool(name, default):
    raw = _pinch_os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean")


def _pinch_env_first(*names, default=""):
    for name in names:
        value = _pinch_os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _pinch_read_response(response, stream):
    if not stream:
        return _pinch_json.loads(response.read().decode("utf-8"))
    raw_chunks = []
    content_chunks = []
    reasoning_chunks = []
    finish_reason = None
    saw_sse = False
    for raw_line in response:
        raw_chunks.append(raw_line)
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        saw_sse = True
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        event = _pinch_json.loads(data)
        if event.get("error"):
            raise RuntimeError(f"PinchBench judge stream failed: {event['error']}")
        choices = event.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or choice.get("message") or {}
        content = delta.get("content")
        if isinstance(content, str):
            content_chunks.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    content_chunks.append(item["text"])
        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str):
            reasoning_chunks.append(reasoning)
        if choice.get("finish_reason") is not None:
            finish_reason = choice["finish_reason"]
    if not saw_sse:
        return _pinch_json.loads(b"".join(raw_chunks).decode("utf-8"))
    return {
        "choices": [{
            "message": {
                "content": "".join(content_chunks),
                "reasoning_content": "".join(reasoning_chunks),
            },
            "finish_reason": finish_reason,
        }]
    }


def _pinch_judge(transcript, workspace_path):
    endpoint = _pinch_env_first(
        "PINCHBENCH_JUDGE_BASE_URL",
        "OPENROUTER_BASE_URL",
        default="https://openrouter.ai/api/v1",
    ).rstrip("/")
    api_key = _pinch_env_first("PINCHBENCH_JUDGE_API_KEY", "OPENROUTER_API_KEY")
    model = _pinch_env_first(
        "PINCHBENCH_JUDGE_MODEL", default=_PINCH_OFFICIAL_JUDGE_MODEL
    )
    if not api_key:
        raise RuntimeError(
            "LLM grading requires PINCHBENCH_JUDGE_API_KEY or OPENROUTER_API_KEY"
        )
    if endpoint.endswith("/v1"):
        endpoint += "/chat/completions"
    elif not endpoint.endswith("/chat/completions"):
        endpoint += "/v1/chat/completions"
    rubric = _PINCH_RUBRIC or "\n".join(
        f"- {criterion}" for criterion in _PINCH_CRITERIA
    )
    request_timeout = _pinch_env_int("PINCHBENCH_JUDGE_TIMEOUT_SECONDS", 300, 1)
    max_tokens = _pinch_env_int(
        "PINCHBENCH_JUDGE_MAX_COMPLETION_TOKENS",
        _pinch_env_int("PINCHBENCH_JUDGE_MAX_TOKENS", 2048, 1),
        1,
    )
    max_tokens_field = _pinch_env_first(
        "PINCHBENCH_JUDGE_MAX_TOKENS_FIELD", default="max_completion_tokens"
    )
    if max_tokens_field not in {"max_completion_tokens", "max_tokens"}:
        raise RuntimeError(
            "PINCHBENCH_JUDGE_MAX_TOKENS_FIELD must be max_completion_tokens or max_tokens"
        )
    # Upstream PinchBench performs one direct judge request.  Keep retries
    # opt-in so the default remains score-comparable while overloaded custom
    # gateways can still request a more forgiving transport policy.
    retries = _pinch_env_int("PINCHBENCH_JUDGE_RETRIES", 0, 0)
    stream = _pinch_env_bool("PINCHBENCH_JUDGE_STREAM", False)
    workspace = _pinch_workspace_text(workspace_path)
    workspace_section = ""
    if workspace.strip():
        workspace_section = f"## Workspace Files Created by Agent\n{workspace}\n\n"
    prompt = (
        "You are a grading function. Your ONLY job is to output a single JSON object.\n\n"
        "CRITICAL RULES FOR YOU, THE GRADER (not the agent being graded):\n"
        "- Do NOT use any tools (no Read, Write, exec, or any other tool calls)\n"
        "- Do NOT create files or run commands\n"
        "- Do NOT write any prose, explanation, or commentary outside the JSON\n"
        "- Respond with ONLY a JSON object — nothing else\n\n"
        "IMPORTANT: The agent being graded may have used tools (read, write, exec, apply_patch, "
        "todowrite, etc.) during task execution. This is normal and expected. Do NOT treat the "
        "agent's tool usage as a rule violation — the rules above apply only to you, the grader.\n\n"
        "Be a strict evaluator. Reserve 1.0 for genuinely excellent performance. "
        "An average acceptable completion should score around 0.6-0.7. "
        "Deduct points for unnecessary steps, verbose output, and inefficient tool usage.\n\n"
        f"## Task\n{_PINCH_TASK_PROMPT}\n\n"
        f"## Expected Behavior\n{_PINCH_EXPECTED}\n\n"
        f"## Agent Transcript (summarized)\n{_pinch_transcript_text(transcript)}\n\n"
        f"{workspace_section}"
        f"## Grading Rubric\n{rubric}\n\n"
        "Score each criterion from 0.0 to 1.0.\n"
        'The "total" field must also be between 0.0 and 1.0, and it must be the '
        "arithmetic mean of the criterion scores, not their sum.\n\n"
        "Respond with ONLY this JSON structure (no markdown, no code fences, no extra text):\n"
        '{"scores": {"criterion_name": 0.0}, "total": 0.0, "notes": "brief justification"}'
    )
    payload_dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": _PINCH_JUDGE_SYSTEM_MSG},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        max_tokens_field: max_tokens,
    }
    if stream:
        payload_dict["stream"] = True
    payload = _pinch_json.dumps(payload_dict).encode("utf-8")
    no_proxy = _pinch_env_bool("PINCHBENCH_JUDGE_NO_PROXY", False)
    opener = _pinch_request.build_opener(
        _pinch_request.ProxyHandler({}) if no_proxy else _pinch_request.ProxyHandler()
    )
    parsed = {}
    last_error = None
    attempts_used = 0
    content_source = None
    finish_reason = None
    for attempt in range(retries + 1):
        attempts_used = attempt + 1
        req = _pinch_request.Request(
            endpoint,
            data=payload,
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener.open(req, timeout=request_timeout) as response:
                body = _pinch_read_response(response, stream)
            choices = body.get("choices", [])
            message = choices[0].get("message", {}) if choices else {}
            text = message.get("content", "")
            content_source = "content"
            if not isinstance(text, str) or not text.strip():
                text = message.get("reasoning_content", "")
                content_source = "reasoning_content"
            finish_reason = choices[0].get("finish_reason") if choices else None
            parsed = _pinch_parse_json(text if isinstance(text, str) else "")
            if _pinch_normalize(parsed)["total"] is not None:
                break
            last_error = ValueError("response contained no parseable score")
        except _pinch_error.HTTPError as exc:
            last_error = exc
        except (_pinch_error.URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
        if attempt < retries:
            _pinch_time.sleep(min(2 ** attempt, 8))
    failure_notes = (
        f"LLM judge failed: no parseable score after {retries + 1} "
        f"attempt(s): {last_error}"
    )
    normalized = _pinch_normalize(parsed)
    scores = normalized["scores"]
    total = normalized["total"]
    notes = normalized["notes"]
    judge_error = None
    if total is None:
        total = 0.0
        notes = failure_notes if not parsed else "LLM judge failed: response parsed but no score extracted"
        judge_error = notes
    provenance = {
        "judge_model": model,
        "judge_protocol": "openai_chat_completions",
        "judge_temperature": 0.0,
        "judge_max_tokens_field": max_tokens_field,
        "judge_max_tokens": max_tokens,
        "judge_stream": stream,
        "judge_attempts": attempts_used,
        "judge_content_source": content_source,
        "judge_finish_reason": finish_reason,
    }
    if judge_error:
        provenance["judge_error"] = notes
    return (
        scores,
        total,
        notes,
        provenance,
    )


def roller_grade(transcript, workspace_path):
    transcript = _pinch_normalize_transcript(transcript)
    automated_scores = {}
    automated_total = 0.0
    if _PINCH_GRADING_TYPE in ("automated", "hybrid"):
        if not callable(_PINCH_AUTOMATED):
            raise RuntimeError("PinchBench task has no automated grade function")
        private_key = _PinchPath(workspace_path) / ".roller-private" / "image_classification_answer_key.json"
        if private_key.is_file():
            globals()["_PINCHBENCH_PRIVATE_IMAGE_KEY_PATH"] = str(private_key)
        automated_scores = _PINCH_AUTOMATED(transcript, workspace_path)
        if not isinstance(automated_scores, dict):
            raise TypeError("PinchBench automated grader must return a dict")
        automated_total = _pinch_average(automated_scores)
    judge_scores, judge_total, notes, judge_provenance = {}, 0.0, "", {}
    if _PINCH_GRADING_TYPE in ("llm_judge", "hybrid"):
        judge_scores, judge_total, notes, judge_provenance = _pinch_judge(
            transcript, workspace_path
        )
    if _PINCH_GRADING_TYPE == "automated":
        result, total = dict(automated_scores), automated_total
    elif _PINCH_GRADING_TYPE == "llm_judge":
        result, total = {"llm_judge." + key: value for key, value in judge_scores.items()}, judge_total
    else:
        auto_weight = float(_PINCH_WEIGHTS.get("automated", 0.5))
        judge_weight = float(_PINCH_WEIGHTS.get("llm_judge", 0.5))
        denominator = auto_weight + judge_weight
        if denominator <= 0:
            auto_weight = judge_weight = 0.5
            denominator = 1.0
        total = (automated_total * auto_weight + judge_total * judge_weight) / denominator
        result = {"automated." + key: value for key, value in automated_scores.items()}
        result.update({"llm_judge." + key: value for key, value in judge_scores.items()})
    result["overall_score"] = total
    result.update(judge_provenance)
    if notes:
        result["judge_notes"] = notes
    judge_error = result.pop("judge_error", None)
    if judge_error:
        result["score_eligible"] = False
        result["error"] = judge_error
    return result
'''


def _grader_source(task: dict[str, Any]) -> str:
    auto = task["automated_checks"]
    if task["grading_type"] in {"automated", "hybrid"} and not auto:
        raise BenchFormatError(
            f"{task['id']}: {task['grading_type']} task has no ## Automated Checks"
        )
    source = (auto.rstrip() + "\n\n") if auto else ""
    replacements = {
        "__TASK_PROMPT__": repr(task["prompt"] or "\n\n".join(
            item["content"] for item in task["sessions"]
        )),
        "__EXPECTED__": repr(task["expected_behavior"]),
        "__CRITERIA__": repr(task["grading_criteria"]),
        "__RUBRIC__": repr(task["llm_judge_rubric"]),
        "__GRADING_TYPE__": repr(task["grading_type"]),
        "__WEIGHTS__": repr(task["grading_weights"]),
    }
    wrapper = _PINCH_WRAPPER
    for marker, value in replacements.items():
        wrapper = wrapper.replace(marker, value)
    return source + wrapper.strip() + "\n"


class PinchBenchAdapter(BenchAdapter):
    name = "pinchbench"
    description = "PinchBench — real-world OpenClaw tasks with automated and LLM grading"
    variants = ("all", "automated-only", "core")
    default_variant = "all"

    @staticmethod
    def _tasks_root(root: Path) -> Path:
        root = root.expanduser().resolve()
        tasks = root / "tasks" if (root / "tasks" / "manifest.yaml").is_file() else root
        if not (tasks / "manifest.yaml").is_file():
            raise FileNotFoundError(f"PinchBench manifest not found under: {root}")
        return tasks

    def load(
        self,
        root: Path,
        *,
        categories: Sequence[str] | None = None,
        task_ids: Sequence[str] | None = None,
        variant: str | None = None,
        timeout_multiplier: float = 1.0,
    ) -> list[dict[str, Any]]:
        tasks_root = self._tasks_root(root)
        timeout_multiplier = validate_timeout_multiplier(timeout_multiplier)
        variant = variant or self.default_variant
        if variant not in self.variants:
            raise BenchFormatError(
                f"{self.name}: variant must be one of {', '.join(self.variants)}"
            )
        manifest = yaml.safe_load((tasks_root / "manifest.yaml").read_text()) or {}
        if not isinstance(manifest, dict) or not isinstance(manifest.get("categories"), dict):
            raise BenchFormatError("pinchbench manifest: categories must be an object")
        ordered: list[tuple[str, str]] = []
        category_by_id: dict[str, str] = {}
        for category, identifiers in manifest["categories"].items():
            if not isinstance(identifiers, list):
                raise BenchFormatError(
                    f"pinchbench manifest: category {category!r} must be a list"
                )
            for task_id in identifiers:
                if not isinstance(task_id, str):
                    raise BenchFormatError("pinchbench manifest: task ids must be strings")
                category_by_id[task_id] = str(category)
                ordered.append((task_id, str(category)))
        first = manifest.get("run_first", [])
        if isinstance(first, list):
            first_ids = [item for item in first if isinstance(item, str)]
            ordered.sort(key=lambda item: first_ids.index(item[0]) if item[0] in first_ids else len(first_ids))
        requested = set(task_ids or ())
        missing = requested - set(category_by_id)
        if missing:
            raise BenchFormatError(
                f"{self.name}: unknown task id(s): {', '.join(sorted(missing))}"
            )
        core = set(manifest.get("core", []))
        category_filter = set(categories or ())
        rows: list[dict[str, Any]] = []
        for task_id, manifest_category in ordered:
            if requested and task_id not in requested:
                continue
            if category_filter and manifest_category not in category_filter:
                continue
            task_path = tasks_root / f"{task_id}.md"
            if not task_path.is_file():
                raise BenchFormatError(f"pinchbench manifest references missing {task_path}")
            task = parse_task_markdown(task_path.read_text(encoding="utf-8"), task_path.name)
            # The manifest is upstream's declared source of truth. A few task
            # frontmatters lag category moves, so use the manifest rather than
            # making an otherwise runnable checkout fail export.
            if variant == "automated-only" and task["grading_type"] != "automated":
                continue
            if variant == "core" and task_id not in core:
                continue
            rows.append(
                self._row(
                    tasks_root.parent,
                    task,
                    manifest_category,
                    variant,
                    timeout_multiplier,
                )
            )
        return rows

    def _row(
        self,
        root: Path,
        task: dict[str, Any],
        category: str,
        variant: str,
        timeout_multiplier: float,
    ) -> dict[str, Any]:
        inputs: list[dict[str, Any]] = []
        for index, item in enumerate(task["workspace_files"]):
            if not isinstance(item, dict):
                raise BenchFormatError(
                    f"{task['id']}: workspace_files[{index}] must be an object"
                )
            if "content" in item:
                target = item.get("path") or item.get("dest")
                if not isinstance(target, str) or not isinstance(item["content"], str):
                    raise BenchFormatError(
                        f"{task['id']}: inline workspace file needs string path/content"
                    )
                inputs.append({"target": target, "content": item["content"]})
            else:
                source, target = item.get("source"), item.get("dest")
                if not isinstance(source, str) or not isinstance(target, str):
                    raise BenchFormatError(
                        f"{task['id']}: workspace file needs string source/dest"
                    )
                inputs.append({"source": source, "target": target})

        stage: list[tuple[str, str]] = []
        private_key = root / "assets" / "image_classification_answer_key.json"
        if task["id"] == "task_image_identification" and private_key.is_file():
            stage.append(
                (str(private_key.resolve()), ".roller-private/image_classification_answer_key.json")
            )
        judge_required = task["grading_type"] in {"llm_judge", "hybrid"}
        grade = GradeSpec(
            code=_grader_source(task),
            entrypoint="roller_grade",
            env_names=JUDGE_ENV if judge_required else (),
            optional_env_names=JUDGE_OPTIONAL_ENV if judge_required else (),
            timeout_seconds=3600 if judge_required else 120,
            stage=tuple(stage),
        )
        binding = BenchBinding(
            self.name,
            grade=grade,
            category=category,
            metadata={
                "name": task["name"],
                "grading_type": task["grading_type"],
                "variant": variant,
                "prerequisites": task["prerequisites"],
            },
        )
        row: dict[str, Any] = {
            "schema_version": "roller.task/v1",
            "id": task["id"],
            "harness": "openclaw",
            "timeout_seconds": max(
                1, math.ceil(task["timeout_seconds"] * timeout_multiplier)
            ),
            "file_root": str((root / "assets").resolve()),
            "files": {
                "inputs": inputs,
                "input_mode": "copy",
                "output_dir": "outputs",
            },
            "bench": binding.as_dict(),
        }
        setup = _pinch_runtime_setup(task)
        if setup:
            row["runtime"] = {"setup": setup}
        if task["sessions"]:
            row["messages"] = task["sessions"]
        else:
            row["prompt"] = task["prompt"]
        return row


__all__ = ["PinchBenchAdapter", "parse_task_markdown"]
