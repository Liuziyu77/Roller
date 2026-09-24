"""Offline repair of Codex tool observations and their SFT projection.

Run with ``python -m roller.reexport_codex RUN --output NEW_DIRECTORY``.
The source run is read-only; the destination contains derived exports only.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from typing import Any

from roller.codex_actf import codex_events_to_actf
from roller.sft import actf_to_sft, validate_sft_record
from roller.storage import atomic_write_json, write_jsonl


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def _observations(trajectory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for step in trajectory["steps"]:
        for observation in step.get("observation", []):
            call_id = observation.get("toolCallId")
            if not isinstance(call_id, str) or not call_id or call_id in result:
                raise ValueError("ACTF observations must have unique, non-empty toolCallId values")
            result[call_id] = observation
    return result


def reexport_sample(sample: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Rebuild observations, preserving recorded conversation and run metadata.

    Older exporters flattened image blocks into text. Start from the native
    trace instead of interpreting arbitrary JSON-looking ACTF text as images.
    Keep captured schemas, prompts, assistant text/reasoning, and metrics from
    the existing ACTF, including any deliberate reasoning removal.
    """
    info = _read_object(sample / "info.json")
    if info.get("harness") != "codex":
        raise ValueError(f"{sample}: only Codex samples can be re-exported")
    original = _read_object(sample / "trace_ACTF.json")
    if original.get("schema_version") != "ACTF_v1.0":
        raise ValueError(f"{sample}: expected an ACTF_v1.0 trajectory")
    with (sample / "trace.jsonl").open(encoding="utf-8") as stream:
        events = [json.loads(line) for line in stream if line.strip()]
    native = _observations(codex_events_to_actf(events))
    repaired = deepcopy(original)
    saved = _observations(repaired)
    if native.keys() != saved.keys():
        raise ValueError(f"{sample}: native and saved ACTF tool-call IDs differ")
    for call_id, observation in saved.items():
        current = native[call_id]
        if observation.get("toolName") != current.get("toolName"):
            raise ValueError(f"{sample}: tool name differs for {call_id}")
        observation["content"] = deepcopy(current["content"])
        observation["isError"] = current["isError"]
    record = actf_to_sft(repaired)
    validate_sft_record(record)
    return repaired, record


def reexport_run(source: Path, destination: Path) -> dict[str, Any]:
    """Export completed Codex task-agent samples into a new directory.

    Fail the batch on conversion errors. A staging directory prevents a
    partially exported dataset from appearing at the requested destination.
    This export is not a resumable Roller run: raw traces, result files, run
    locks, and User Simulator trajectories remain in the source directory.
    """
    source = source.expanduser().resolve()
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"destination already exists: {destination}")
    if destination.resolve().is_relative_to(source):
        raise ValueError("destination must be outside the source run")
    info = _read_object(source / "info.json")
    samples = info.get("samples")
    if not isinstance(samples, list):
        raise ValueError("run info.json must contain a samples list")
    selected = sorted(
        (entry for entry in samples if entry.get("status") in {"ok", "skipped"}),
        key=lambda entry: entry["request_index"],
    )
    if not selected:
        raise ValueError("run contains no completed samples")
    destination.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema_version": "roller.codex-reexport/v1",
        "source_run": str(source),
        "skipped_samples": len(samples) - len(selected),
        "samples": [],
    }
    with tempfile.TemporaryDirectory(prefix=".codex-reexport-", dir=destination.parent) as tmp:
        staging = Path(tmp) / "export"
        staging.mkdir()
        seen: set[Path] = set()

        def records():
            for entry in selected:
                relative = Path(entry["path"])
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or relative.parts[:1] != ("samples",)
                ):
                    raise ValueError(f"invalid sample path: {relative}")
                sample = (source / relative).resolve()
                if not sample.is_relative_to(source) or sample in seen:
                    raise ValueError(f"sample path escapes the run or is duplicated: {relative}")
                seen.add(sample)
                trajectory, record = reexport_sample(sample)
                atomic_write_json(staging / relative / "trace_ACTF.json", trajectory)
                atomic_write_json(staging / relative / "trace_SFT.json", record)
                report["samples"].append({
                    "line_number": len(report["samples"]) + 1,
                    "request_index": entry["request_index"],
                    "task_id": entry["task_id"],
                    "model": entry["model"],
                    "harness": "codex",
                    "sample_path": str(relative),
                    "image_count": len(record.get("images", [])),
                })
                yield record

        write_jsonl(staging / "sft.jsonl", records())
        atomic_write_json(staging / "reexport.json", report)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"destination already exists: {destination}")
        staging.rename(destination)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="existing Roller Codex run directory")
    parser.add_argument("--output", type=Path, required=True, help="new derived-export directory")
    args = parser.parse_args()
    try:
        report = reexport_run(args.run, args.output)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"Codex re-export failed: {exc}\n")
    print(f"Re-exported {len(report['samples'])} samples to {args.output}")


if __name__ == "__main__":
    main()
