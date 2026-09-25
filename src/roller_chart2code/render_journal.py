"""Read-only validation and conservative ACTF association of render journals."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

SCHEMA = "roller.render-attempt/v1"
MARKER = re.compile(r"\[roller-render (attempt_\d{6,}) status=([a-z_]+)\]")
STATUSES = {"success", "execution_failed", "missing_image", "invalid_image",
            "script_changed", "interrupted", "recorder_error"}


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _safe(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or any(c in relative for c in "\n\r"):
        raise ValueError("invalid journal path")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("journal path escapes results")
    current = root
    if root.is_symlink():
        raise ValueError("results is a symlink")
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"journal symlink: {relative}")
    return current


def _object(path: Path) -> dict:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError(f"oversized journal metadata: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path.name}")
    return value


def _text(value):
    # Only observation text; never search assistant reasoning or image payloads.
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _text(item)
    elif isinstance(value, dict):
        if isinstance(value.get("text"), str):
            yield value["text"]


def _links(trajectory: dict | None) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    if not isinstance(trajectory, dict) or not isinstance(trajectory.get("steps"), list):
        return result
    for step in trajectory["steps"]:
        if not isinstance(step, dict):
            continue
        assistant = step.get("assistant_content")
        if not isinstance(assistant, dict):
            continue
        calls = assistant.get("tool_calls")
        if not isinstance(calls, list) or not isinstance(step.get("observation"), list):
            continue
        call_ids = {call.get("id") for call in calls if isinstance(call, dict)}
        for observation in step.get("observation", []):
            if not isinstance(observation, dict):
                continue
            call_id = observation.get("toolCallId") or observation.get("tool_call_id")
            if not call_id or call_id not in call_ids:
                continue
            for text in _text(observation.get("content", [])):
                for attempt_id, status in MARKER.findall(text):
                    link = {"step_id": step.get("step_id"), "tool_call_id": call_id, "status": status}
                    if link not in result.setdefault(attempt_id, []):
                        result[attempt_id].append(link)
    return result


def validate_render_journal(results: Path, trajectory: dict | None = None) -> dict[str, Any] | None:
    """None means a legacy/non-Chart2Code result with no journal evidence.

    A failed attempt is valid evidence. Qualification additionally requires a
    success and final files matching the last successful attempt. Association
    is advisory: missing/truncated tool output must not create guessed links.
    """
    iterations = results / "iterations"
    manifest = iterations / "render_manifest.jsonl"
    if not manifest.exists() and not manifest.is_symlink() and not any(iterations.glob("attempt_*")):
        return None
    report: dict[str, Any] = {"schema_version": "roller.render-validation/v1", "passed": False,
                              "errors": [], "attempt_count": 0, "success_count": 0, "attempts": []}
    links = _links(trajectory)
    seen, steps = set(), set()
    last_success = None
    try:
        manifest = _safe(results, "iterations/render_manifest.jsonl")
        with manifest.open(encoding="utf-8") as stream:
            index = 0
            while True:
                line = stream.readline(1024 * 1024 + 1)
                if not line:
                    break
                index += 1
                if len(line) > 1024 * 1024:
                    raise ValueError("oversized journal row")
                row = json.loads(line)
                if not isinstance(row, dict) or row.get("schema_version") != SCHEMA:
                    raise ValueError(f"invalid attempt schema at row {index}")
                attempt_id = row.get("attempt_id", "")
                if not re.fullmatch(r"attempt_\d{6,}", attempt_id) or attempt_id in seen:
                    raise ValueError(f"invalid/duplicate attempt ID at row {index}")
                if row.get("attempt") != index or int(attempt_id[8:]) != index:
                    raise ValueError("attempt sequence is incomplete")
                seen.add(attempt_id)
                prefix = f"iterations/{attempt_id}"
                metadata = _object(_safe(results, f"{prefix}/metadata.json"))
                if metadata != row:
                    raise ValueError(f"metadata/manifest mismatch: {attempt_id}")
                status = row.get("status")
                if status not in STATUSES or not row.get("finished_at") or not row.get("started_at"):
                    raise ValueError(f"incomplete attempt: {attempt_id}")
                files = row.get("files")
                if not isinstance(files, dict):
                    raise ValueError(f"invalid files: {attempt_id}")
                required = {"plot.py", "stdout.log", "stderr.log"} if status != "recorder_error" else set()
                if status == "success":
                    required.add("output.png")
                    if row.get("exit_code") != 0 or row.get("wrapper_exit_code") != 0:
                        raise ValueError(f"success has nonzero exit: {attempt_id}")
                if not required <= files.keys():
                    raise ValueError(f"missing evidence: {attempt_id}")
                for filename, entry in files.items():
                    if filename not in {"plot.py", "output.png", "candidate.png", "stdout.log", "stderr.log"}:
                        raise ValueError(f"unexpected attempt file: {filename}")
                    path = _safe(results, f"{prefix}/{filename}")
                    if not isinstance(entry, dict) or path.stat().st_size != entry.get("size") or _digest(path) != entry.get("sha256"):
                        raise ValueError(f"file hash/size mismatch: {attempt_id}/{filename}")
                if status == "success":
                    step = row.get("step", "")
                    if not re.fullmatch(r"step_\d{2,}", step) or step in steps:
                        raise ValueError(f"invalid/duplicate success step: {attempt_id}")
                    steps.add(step)
                    for suffix, filename in ((".py", "plot.py"), (".png", "output.png")):
                        if _digest(_safe(results, f"iterations/{step}{suffix}")) != files[filename]["sha256"]:
                            raise ValueError(f"legacy step mismatch: {step}{suffix}")
                    last_success = row
                    report["success_count"] += 1
                elif row.get("step") is not None or (row.get("wrapper_exit_code") == 0 and row.get("recorder_version", 1) < 2):
                    raise ValueError(f"failed attempt has success fields: {attempt_id}")
                candidates = links.get(attempt_id, [])
                matched = len(candidates) == 1 and candidates[0]["status"] == status
                report["attempts"].append({"attempt_id": attempt_id, "status": status,
                    "metadata": f"{prefix}/metadata.json",
                    "association": "matched" if matched else ("ambiguous" if candidates else "unmatched"),
                    "tool_call": candidates[0] if matched else None})
                report["attempt_count"] += 1
        on_disk = {p.name for p in iterations.glob("attempt_*")}
        if seen != on_disk:
            raise ValueError("uncommitted or missing attempt directories")
        if not last_success:
            raise ValueError("no successful render in journal")
        for filename in ("plot.py", "output.png"):
            if _digest(_safe(results, filename)) != last_success["files"][filename]["sha256"]:
                raise ValueError(f"final {filename} differs from last successful render")
        report["passed"] = True
    except (OSError, ValueError, TypeError, KeyError) as exc:
        report["errors"].append(str(exc))
    return report
