"""Read current evidence before every task audit/export; never trust old PASS."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from roller.storage import atomic_write_json, write_jsonl
from roller_chart2code.acceptance import audit_sample, file_hash, json_object, safe_root, write_report
from roller_chart2code.render_journal import _safe


def entries_for_run(run_dir):
    manifest = json_object(_safe(run_dir, "chart2code/contracts.json"))
    if manifest.get("schema_version") != "roller.chart2code-contracts/v1":
        raise ValueError("unsupported Chart2Code contracts manifest")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("contracts manifest must contain samples")
    run_info = json_object(_safe(run_dir, "info.json"))
    source_rows = {}
    for row in run_info.get("samples", []):
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise ValueError("invalid run sample metadata")
        path = row["path"]
        _safe(run_dir, path)
        if path in source_rows:
            raise ValueError("duplicate run sample path")
        source_rows[path] = row
    seen = set()
    for entry in entries:
        path = entry["sample_path"]
        if _safe(run_dir, path).relative_to(run_dir).as_posix() != path or not path.startswith("samples/"):
            raise ValueError("sample path must be canonical and under samples/")
        if path in seen:
            raise ValueError("duplicate contracted sample path")
        seen.add(path)
        source = source_rows.get(path)
        if source is None:
            # Interrupted runs retain unprocessed contracts, but cannot qualify them.
            yield entry, None
            continue
        for key in ("request_index", "task_id", "model", "model_label", "sample_index", "sample_count"):
            expected = entry["contract"]["identity"].get(key)
            if expected is not None and source.get(key) != expected:
                raise ValueError("run sample identity disagrees with frozen contract")
        yield entry, source
    if source_rows.keys() - seen:
        raise ValueError("run contains samples absent from the task contract manifest")


def audit_run(run_dir: Path, *, report_only=False) -> dict:
    run_dir = safe_root(run_dir)
    rows = []
    for entry, source in entries_for_run(run_dir):
        path = entry["sample_path"]
        try:
            report = audit_sample(_safe(run_dir, path), entry["contract"], report_only=report_only)
            if source is None or source.get("status") not in {"ok", "skipped"}:
                report["qualified"] = False
                report["run_metadata_error"] = "sample is unprocessed or run execution status is not successful"
            target = write_report(run_dir, path, report)
            rows.append({"sample_path": path, "qualified": report["qualified"],
                "source_run_status": source.get("status") if source else "unprocessed",
                "fingerprint": report["fingerprint"], "profile_verdict": report["profile_verdict"],
                "failed_checks": [check["id"] for gate in report["gates"].values() for check in gate["checks"] if not check["passed"]],
                "report": target.relative_to(run_dir).as_posix()})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            rejected = {"schema_version": "roller.chart2code-audit-error/v1", "qualified": False, "error": str(exc)}
            # Supersede any old PASS even if current evidence cannot be parsed.
            target = write_report(run_dir, path, rejected)
            rows.append({"sample_path": path, **rejected, "report": target.relative_to(run_dir).as_posix()})
    summary = {"schema_version": "roller.chart2code-summary/v1", "report_only": report_only,
               "total": len(rows), "qualified": sum(row["qualified"] for row in rows), "samples": rows}
    summary["state"] = "qualified" if summary["qualified"] == len(rows) and rows else "not_qualified"
    atomic_write_json(_safe(run_dir, "chart2code/summary.json"), summary)
    return summary


def export_run(run_dir: Path, output: Path) -> dict:
    """Publish to a NEW directory; rejected samples remain listed, not exported.

    Failed staging directories are preserved for diagnosis. No historical run
    or previously published dataset is overwritten. Images remain inline under
    the agreed upstream-compatible SFT schema.
    """
    run_dir, output = safe_root(run_dir), safe_root(output)
    if any(c in str(output) for c in "\r\n") or output.is_relative_to(run_dir) or run_dir.is_relative_to(output):
        raise ValueError("dataset output must be a separate, single-line path")
    for parent in (output, *output.parents):
        if parent.is_symlink():
            raise ValueError("dataset output cannot use symlink paths")
    if output.exists():
        raise FileExistsError("dataset output already exists; choose a new directory")
    summary = audit_run(run_dir)
    if not summary["qualified"]:
        raise ValueError("no qualified samples; dataset not published (see chart2code/summary.json)")
    contracts = {entry["sample_path"]: entry["contract"] for entry, _ in entries_for_run(run_dir)}
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent))
    selected = []
    def records():
        for row in summary["samples"]:
            if not row["qualified"]:
                continue
            sample = _safe(run_dir, row["sample_path"])
            record = json_object(_safe(sample, "trace_SFT.json"))
            report = audit_sample(sample, contracts[row["sample_path"]])
            if not report["qualified"] or report["fingerprint"] != row["fingerprint"]:
                raise ValueError("evidence changed during export; staged output was not published")
            # Check the bytes read for this row against the current re-audited SFT.
            current = json_object(_safe(sample, "trace_SFT.json"))
            if record != current:
                raise ValueError("SFT changed during export")
            selected.append(row)
            yield record
    try:
        write_jsonl(staging / "sft.jsonl", records())
        manifest = {"schema_version": "roller.chart2code-dataset/v1", "source_run": str(run_dir),
                    "count": len(selected), "sft_sha256": file_hash(staging / "sft.jsonl"),
                    "selected": selected, "excluded": [r for r in summary["samples"] if not r["qualified"]]}
        atomic_write_json(staging / "manifest.json", manifest)
        if output.exists():
            raise FileExistsError("dataset appeared during export; refusing to overwrite")
        os.rename(staging, output)
        return manifest
    except BaseException as exc:
        if hasattr(exc, "add_note"):
            exc.add_note(f"Unpublished staging evidence retained at {staging}")
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline Chart2Code audit/export; no Docker, API or collected-code execution")
    parser.add_argument("mode", choices=("audit", "export"))
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args(argv)
    if any(c in str(args.run_dir) for c in "\r\n"):
        parser.error("run directory must be single-line")
    if args.mode == "export" and (args.output is None or args.report_only):
        parser.error("export requires --output and forbids --report-only")
    try:
        result = audit_run(args.run_dir, report_only=args.report_only) if args.mode == "audit" else export_run(args.run_dir, args.output)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Chart2Code rejected: {exc}\n")
    print(json.dumps({k: v for k, v in result.items() if k not in {"samples", "selected", "excluded"}}, ensure_ascii=False, indent=2))
    return 0 if args.mode == "export" or result["state"] == "qualified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
