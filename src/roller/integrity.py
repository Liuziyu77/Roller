"""Integrity checks for finalized samples considered for resume."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from roller.models import RunRequest
from roller.capture import validate_capture
from roller.claude_code_actf import _read_events as read_claude_events
from roller.harness_registry import requires_cache_trace
from roller.sft import actf_to_sft, validate_sft_record


def trajectory_conversion_unavailable(
    metadata: Mapping[str, Any] | None,
) -> str | None:
    """Return the unavailable trajectory target explicitly recorded in metadata.

    A warning string is deliberately insufficient: callers must provide a
    structured, non-empty conversion error. Older OpenCode results emitted no
    target and are interpreted as ACTF failures.
    """

    if not isinstance(metadata, Mapping):
        return None
    conversion = metadata.get("trajectory_conversion")
    valid = (
        isinstance(conversion, Mapping)
        and conversion.get("status") == "unavailable"
        and isinstance(conversion.get("error"), str)
        and bool(conversion["error"].strip())
    )
    if not valid:
        return None
    target = conversion.get("target", "actf")
    return target if target in {"actf", "sft"} else None


def has_legacy_claude_sidechain(trajectory: Mapping[str, Any]) -> bool:
    """Identify older ACTF records that mixed child steps into the root."""

    steps = trajectory.get("steps")
    return isinstance(steps, list) and any(
        isinstance(step, Mapping)
        and isinstance(step.get("extra"), Mapping)
        and step["extra"].get("is_sidechain") is True
        for step in steps
    )


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name} is not readable JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _has_json_event(path: Path) -> bool:
    """Return whether a harness-native JSONL trace contains one JSON object."""

    try:
        with path.open("r", encoding="utf-8", errors="replace") as source:
            for line in source:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    return True
    except OSError as exc:
        raise ValueError(f"{path.name} cannot be read: {exc}") from exc
    return False


def _validate_output_files(sample_dir: Path, info: Mapping[str, Any]) -> None:
    output = info.get("output_collection")
    if not isinstance(output, Mapping) or not output.get("passed"):
        return
    rows = output.get("files", [])
    if not isinstance(rows, list):
        raise ValueError("info.json output_collection.files must be a list")
    results_root = sample_dir / "results"
    if results_root.is_symlink():
        raise ValueError("sample results/ directory must not be a symlink")
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"output_collection.files[{index}] must be an object")
        relative = row.get("path")
        if not isinstance(relative, str) or not relative:
            raise ValueError(f"output_collection.files[{index}].path must be non-empty")
        relative_path = Path(relative)
        if relative_path.is_absolute() or any(
            part in {"", ".", ".."} for part in relative_path.parts
        ):
            raise ValueError(
                f"output_collection.files[{index}].path is not canonical: {relative}"
            )
        unresolved = results_root / relative_path
        current = results_root
        for part in relative_path.parts:
            current /= part
            if current.is_symlink():
                raise ValueError(f"recorded output path contains a symlink: {relative}")
        candidate = unresolved.resolve()
        try:
            candidate.relative_to(results_root.resolve())
        except ValueError as exc:
            raise ValueError(
                f"output_collection.files[{index}].path escapes results/: {relative}"
            ) from exc
        if not candidate.is_file():
            raise ValueError(f"recorded output file is missing: results/{relative}")
        expected_size = row.get("size")
        if isinstance(expected_size, int) and not isinstance(expected_size, bool):
            try:
                actual_size = candidate.stat().st_size
            except OSError as exc:
                raise ValueError(
                    f"recorded output file cannot be inspected: results/{relative}"
                ) from exc
            if actual_size != expected_size:
                raise ValueError(f"recorded output file size changed: results/{relative}")


def _validate_trajectory_files(
    base: Path,
    info: Mapping[str, Any],
    *,
    require_wire_trace: bool,
    require_cache_trace: bool,
) -> None:
    raw_path = base / "trace.jsonl"
    actf_path = base / "trace_ACTF.json"
    sft_path = base / "trace_SFT.json"
    if not raw_path.is_file():
        raise ValueError(f"required trajectory file is missing: {raw_path.name}")
    runtime = info.get("runtime")
    conversion_target = trajectory_conversion_unavailable(
        runtime if isinstance(runtime, Mapping) else None
    )
    raw_only = (
        info.get("harness") in {"opencode", "openclaw"}
        and conversion_target == "actf"
    )
    sft_unavailable = conversion_target == "sft"
    if raw_only:
        trajectory = info.get("trajectory")
        if not isinstance(trajectory, Mapping):
            raise ValueError("info.json trajectory must be an object")
        if trajectory.get("actf") is not None or trajectory.get("sft") is not None:
            raise ValueError(
                "raw-only trajectory metadata must not reference ACTF/SFT artifacts"
            )
        if actf_path.exists() or sft_path.exists():
            raise ValueError("raw-only sample contains unexpected ACTF/SFT artifacts")
    else:
        if not actf_path.is_file():
            raise ValueError(f"required trajectory file is missing: {actf_path.name}")
        if sft_unavailable:
            trajectory = info.get("trajectory")
            if not isinstance(trajectory, Mapping) or trajectory.get("sft") is not None:
                raise ValueError("SFT-unavailable trajectory metadata must have null SFT")
            if sft_path.exists():
                raise ValueError("SFT-unavailable sample contains an unexpected SFT artifact")
        elif not sft_path.is_file():
            raise ValueError(f"required trajectory file is missing: {sft_path.name}")
    if require_wire_trace and not (base / "wire_trace.jsonl").is_file():
        raise ValueError("required full-mode wire_trace.jsonl is missing")
    # Only OpenClaw emits cache traces. Finalized metadata records the actual
    # harness, including CLI overrides, for each participant independently.
    # Keep the strict check for legacy or unrecognized harness metadata.
    cache_trace_required = require_cache_trace and requires_cache_trace(info.get("harness"))
    if cache_trace_required and not (base / "cache_trace.jsonl").is_file():
        raise ValueError("required full-mode cache_trace.jsonl is missing")
    if not _has_json_event(raw_path):
        raise ValueError("trace.jsonl contains no valid JSON object event")
    capture = runtime.get("capture") if isinstance(runtime, Mapping) else None
    if isinstance(capture, dict):
        validate_capture(base, capture)
    if info.get("harness") == "claude-code" and not sft_unavailable:
        if any(row.get("subtype") == "compact_boundary" for row in read_claude_events(raw_path)):
            raise ValueError("Claude compaction requires per-call context export, not linear SFT")

    if raw_only:
        return

    actf = _json_object(actf_path)
    if info.get("harness") == "claude-code" and has_legacy_claude_sidechain(actf):
        raise ValueError(
            "legacy Claude Code sidechain steps are mixed into root ACTF/SFT; "
            "the sample cannot be safely reused or silently rerun"
        )
    if actf.get("schema_version") != "ACTF_v1.0":
        raise ValueError("trace_ACTF.json has an unsupported schema_version")
    steps = actf.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("trace_ACTF.json steps must be a non-empty list")
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, Mapping) or step.get("step_id") != index:
            raise ValueError(
                "trace_ACTF.json step_id values must be sequential from 1"
            )

    if not sft_unavailable:
        actual_sft = _json_object(sft_path)
        try:
            validate_sft_record(actual_sft)
        except ValueError as exc:
            raise ValueError(f"trace_SFT.json is not a valid SFT record: {exc}") from exc
        expected_sft = actf_to_sft(actf)
        if actual_sft != expected_sft:
            raise ValueError("trace_SFT.json does not match the canonical ACTF projection")


def validate_resumable_sample(
    sample_dir: Path,
    request: RunRequest,
    info: Mapping[str, Any],
    *,
    require_wire_trace: bool = False,
    require_cache_trace: bool = False,
) -> None:
    """Raise ``ValueError`` when a finalized sample is unsafe to reuse.

    Cache-trace requirements are resolved separately for each participant's
    recorded harness; wire-trace requirements apply to every harness.
    """

    if info.get("schema_version") != "roller.sample/v1":
        raise ValueError("info.json has an unsupported schema_version")
    if info.get("status") != "ok":
        raise ValueError("info.json status is not ok")
    expected_identity = {
        "request_index": request.index,
        "sample_index": request.sample_index,
        "sample_count": request.sample_count,
        "source_index": request.task.source_index,
        "task_id": request.task.id,
        "model": request.model,
    }
    legacy_defaults = {"sample_index": 0, "sample_count": 1}
    for key, expected in expected_identity.items():
        if info.get(key, legacy_defaults.get(key)) != expected:
            raise ValueError(
                f"info.json {key} does not match current request: "
                f"{info.get(key)!r} != {expected!r}"
            )

    _validate_trajectory_files(
        sample_dir,
        info,
        require_wire_trace=require_wire_trace,
        require_cache_trace=require_cache_trace,
    )
    _validate_output_files(sample_dir, info)

    if request.task.user_simulator is None:
        return
    conversation = _json_object(sample_dir / "conversation.json")
    if conversation.get("schema_version") != "roller.conversation/v1":
        raise ValueError("conversation.json has an unsupported schema_version")
    if conversation.get("status") != "ok":
        raise ValueError("conversation.json status is not ok")
    simulator_dir = sample_dir / "user_simulator"
    simulator_info = _json_object(simulator_dir / "info.json")
    if simulator_info.get("schema_version") != "roller.participant/v1":
        raise ValueError("user_simulator/info.json has an unsupported schema_version")
    if simulator_info.get("status") != "ok":
        raise ValueError("user_simulator/info.json status is not ok")
    _validate_trajectory_files(
        simulator_dir,
        simulator_info,
        require_wire_trace=require_wire_trace,
        require_cache_trace=require_cache_trace,
    )
