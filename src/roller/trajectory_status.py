"""Task-independent conversion outcomes; never rewrite execution status."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from roller.sft import SFTConversionError, write_sft


def export_sft(base: Path, metadata: dict[str, Any]) -> None:
    """Export an archived ACTF, recording failures separately from execution."""
    destination = base / "trace_SFT.json"
    try:
        trajectory = json.loads((base / "trace_ACTF.json").read_text(encoding="utf-8"))
        if not isinstance(trajectory, dict) or trajectory.get("schema_version") != "ACTF_v1.0":
            raise ValueError("canonical ACTF trajectory must have schema ACTF_v1.0")
        steps = trajectory.get("steps")
        if not isinstance(steps, list) or not steps or any(
            not isinstance(step, dict) or step.get("step_id") != index
            for index, step in enumerate(steps, 1)
        ):
            raise ValueError("canonical ACTF steps must be non-empty and sequential from 1")
        write_sft(destination, trajectory)
    except Exception as exc:
        metadata["trajectory_conversion"] = {
            "status": "unavailable" if isinstance(exc, SFTConversionError) else "failed",
            "target": "sft", "error": str(exc),
        }
        destination.unlink(missing_ok=True)


def conversion_outcome(base: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    if not (base / "trace.jsonl").is_file():
        return {"status": "failed", "target": "raw", "error": "required trace.jsonl is missing"}
    from roller.integrity import _has_json_event
    if not _has_json_event(base / "trace.jsonl"):
        return {"status": "failed", "target": "raw", "error": "trace.jsonl contains no valid JSON object"}
    conversion = metadata.get("trajectory_conversion")
    if isinstance(conversion, dict) and conversion.get("status") in {"unavailable", "failed"}:
        return dict(conversion)
    for target, filename in (("raw", "trace.jsonl"), ("actf", "trace_ACTF.json"), ("sft", "trace_SFT.json")):
        if not (base / filename).is_file():
            return {"status": "failed", "target": target, "error": f"required {filename} is missing"}
    return {"status": "ok", "target": "sft"}


def training_outcome(status: str, conversion: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    if status != "ok":
        reasons.append("execution_not_ok")
    if conversion.get("status") != "ok":
        reasons.append(f"{conversion.get('target', 'trajectory')}_{conversion.get('status', 'unknown')}")
    return {"eligible": not reasons, "reasons": reasons}
