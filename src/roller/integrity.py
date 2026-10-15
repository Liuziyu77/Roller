"""Integrity checks for finalized samples considered for resume."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from roller.models import RunRequest
from roller.capture import validate_capture
from roller.compaction_sft import SFT_SEGMENTS_DIR, build_compaction_segments, has_main_compaction
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
    segment_dir = base / SFT_SEGMENTS_DIR
    trajectory = info.get("trajectory")
    segments = trajectory.get("sft_segments") if isinstance(trajectory, Mapping) else None
    if segments is not None and (not isinstance(segments, list) or not segments):
        raise ValueError("SFT segment metadata must be a non-empty list")
    if not segments and segment_dir.exists():
        raise ValueError("unreferenced SFT segment directory is present")
    if segments and trajectory.get("sft") is not None:
        raise ValueError("compacted sample cannot also reference a linear SFT")
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
    if segments and sft_unavailable:
        raise ValueError("SFT-unavailable sample cannot reference SFT segments")
    if raw_only:
        if segments:
            raise ValueError("raw-only sample cannot contain SFT segments")
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
        elif not segments and not sft_path.is_file():
            raise ValueError(f"required trajectory file is missing: {sft_path.name}")
        elif segments and sft_path.exists():
            raise ValueError("compacted sample must not contain a linear SFT")
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
    if not sft_unavailable and not segments and has_main_compaction(raw_path, info.get("harness")):
        raise ValueError("compaction requires per-call context export, not linear SFT")

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

    if segments and not sft_unavailable:
        if info.get("harness") not in {"claude-code", "codex"}:
            raise ValueError("unsupported harness for SFT segments")
        capture_info = capture if isinstance(capture, Mapping) else {}
        manifest = capture_info.get("manifest")
        if not isinstance(manifest, str):
            raise ValueError("SFT segments lack their captured Calls")
        expected = build_compaction_segments(base, info["harness"], manifest)
        if segments != [metadata for metadata, _ in expected]:
            raise ValueError("SFT segment metadata does not match captured boundaries and Calls")
        expected_paths = {item["path"] for item in segments}
        if not segment_dir.is_dir() or segment_dir.is_symlink():
            raise ValueError("SFT segment directory is missing or invalid")
        files = list(segment_dir.iterdir())
        if any(not path.is_file() or path.is_symlink() for path in files):
            raise ValueError("SFT segment directory contains an invalid entry")
        actual_paths = {path.relative_to(base).as_posix() for path in files}
        if actual_paths != expected_paths:
            raise ValueError("SFT segment files do not match metadata")
        for metadata, record in expected:
            actual = _json_object(base / metadata["path"])
            validate_sft_record(actual)
            if actual != record:
                raise ValueError("SFT segment does not match captured Call context")
    elif not sft_unavailable:
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
