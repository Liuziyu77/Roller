"""Integrity checks for finalized samples considered for resume."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from roller.external_thinking import external_thinking_mode_from_metadata
from roller.models import RunRequest
from roller.harness_registry import requires_cache_trace
from roller.sft import actf_to_sft, validate_sft_record


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
    for path in (raw_path, actf_path, sft_path):
        if not path.is_file():
            raise ValueError(f"required trajectory file is missing: {path.name}")
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

    actf = _json_object(actf_path)
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

    actual_sft = _json_object(sft_path)
    try:
        validate_sft_record(actual_sft)
    except ValueError as exc:
        raise ValueError(f"trace_SFT.json is not a valid SFT record: {exc}") from exc
    runtime = info.get("runtime")
    mode = external_thinking_mode_from_metadata(
        runtime if isinstance(runtime, Mapping) else None
    )
    expected_sft = actf_to_sft(actf, external_thinking=mode)
    if actual_sft != expected_sft:
        raise ValueError("trace_SFT.json does not match the canonical ACTF projection")

    recovery = runtime.get("signature_recovery") if isinstance(runtime, Mapping) else None
    if isinstance(recovery, Mapping) and recovery.get("mode") in {"fill", "prefer"}:
        recovery_path = base / "signature_recovery.jsonl"
        if not recovery_path.is_file():
            raise ValueError("required signature_recovery.jsonl is missing")
        rows: list[dict[str, Any]] = []
        try:
            with recovery_path.open("r", encoding="utf-8") as source:
                for line_number, line in enumerate(source, start=1):
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError(
                            f"signature_recovery.jsonl line {line_number} must be an object"
                        )
                    if value.get("schema_version") != (
                        "roller.claude-signature-recovery/v1"
                    ):
                        raise ValueError(
                            "signature_recovery.jsonl has an unsupported schema_version"
                        )
                    if "signature" in value:
                        raise ValueError(
                            "signature_recovery.jsonl must not duplicate signature values"
                        )
                    rows.append(value)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"signature_recovery.jsonl is not readable JSONL: {exc}"
            ) from exc
        expected_count = recovery.get("candidate_count")
        if isinstance(expected_count, int) and len(rows) != expected_count:
            raise ValueError(
                "signature_recovery.jsonl candidate count does not match info.json"
            )
        recovered_count = sum(row.get("status") == "ok" for row in rows)
        expected_recovered = recovery.get("recovered_count")
        if isinstance(expected_recovered, int) and recovered_count != expected_recovered:
            raise ValueError(
                "signature_recovery.jsonl recovered count does not match info.json"
            )


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
        "source_index": request.task.source_index,
        "task_id": request.task.id,
        "model": request.model,
    }
    for key, expected in expected_identity.items():
        if info.get(key) != expected:
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
