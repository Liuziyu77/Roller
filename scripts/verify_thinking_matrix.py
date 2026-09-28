"""Run the issue #51 online matrix without putting API keys in arguments."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
HARNESSES = ("hermes", "opencode", "deepseek", "codex", "claude-code", "pi", "openclaw")
MODELS = ("qwen3.8-flash", "glm-5.3-flash", "kimi-k2.7-code-highspeed",
          "deepseek-v4-flash", "gpt-5.6-luna", "claude-opus-4-8", "gemini-3.8-flash")
EFFORTS = ("off", "low", "high", "max")


def contains_token(path: Path, needle: bytes) -> bool:
    overlap = b""
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            data = overlap + chunk
            if needle in data:
                return True
            overlap = data[-(len(needle) - 1):] if len(needle) > 1 else b""
    return False


def inputs(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for effort in EFFORTS:
        marker = f"EFFORT_{effort.upper()}_OK"
        task = {
            "schema_version": "roller.task/v1", "id": f"effort_{effort}",
            "prompt": (
                f"Use your local shell/terminal/bash tool to write exactly {marker} "
                "to outputs/answer.txt. Then use a tool to read that file and verify it. "
                f"Your final answer must be exactly {marker} with no extra explanation. "
                "Do not use subagents, network access or install dependencies."
            ),
            "runtime": {"thinking": effort}, "files": {"output_dir": "outputs"},
        }
        (root / f"tasks-{effort}.jsonl").write_text(json.dumps(task) + "\n")
    config = {"mode": "merge", "providers": {"matrix": {
        "baseUrl": "${ROLLER_MATRIX_BASE_URL}", "apiKey": "${ROLLER_MATRIX_API_KEY}",
        "api": "openai-completions",
        "models": [{"id": model, "name": model, "reasoning": True} for model in MODELS],
    }}}
    (root / "openclaw-models.json").write_text(json.dumps(config, indent=2) + "\n")


def result_rows(root: Path, phase: str) -> list[dict]:
    rows = []
    for harness in HARNESSES:
        for effort in EFFORTS:
            run = root / phase / f"{harness}-{effort}"
            index = run / "info.json"
            if not index.is_file():
                continue
            for sample in json.loads(index.read_text()).get("samples", []):
                folder = run / sample["path"]
                detail_path = folder / "info.json"
                if not detail_path.is_file():
                    continue
                detail = json.loads(detail_path.read_text())
                runtime = detail.get("runtime") or {}
                marker = f"EFFORT_{effort.upper()}_OK"
                output = folder / "results" / "answer.txt"
                answer = output.read_text(errors="replace").strip() if output.is_file() else None
                actf_path = folder / "trace_ACTF.json"
                final, calls, observations, ids_match = None, 0, 0, True
                if actf_path.is_file():
                    steps = json.loads(actf_path.read_text()).get("steps") or []
                    if steps:
                        final = (steps[-1].get("assistant_content") or {}).get("content")
                    for step in steps:
                        tool_calls = (step.get("assistant_content") or {}).get("tool_calls") or []
                        results = step.get("observation") or []
                        calls += len(tool_calls)
                        observations += len(results)
                        ids_match &= {c.get("id") for c in tool_calls} == {
                            r.get("toolCallId") for r in results}
                wire = runtime.get("wire_effort_audit") or {}
                row = {
                    "harness": harness, "model": sample.get("model"), "effort": effort,
                    "status": detail.get("status"),
                    "task_ok": answer == marker and final == marker,
                    "tools_ok": calls >= 1 and observations >= 1 and ids_match,
                    "tool_calls": calls, "tool_results": observations,
                    "wire_status": wire.get("status"), "wire_source": wire.get("source"),
                    "visible_status": (runtime.get("thinking_audit") or {}).get("status"),
                    "completion_status": (runtime.get("completion_audit") or {}).get("status"),
                    "response_completion_status": (runtime.get("response_completion_audit") or {}).get("status"),
                    "sft_exclusion_reason": (runtime.get("sft_eligibility") or {}).get("reason"),
                    "sample_sft": (folder / "trace_SFT.json").is_file(),
                    "path": folder.relative_to(root).as_posix(),
                }
                if row["wire_status"] == "mismatch" or row["visible_status"] == "mismatch":
                    row["mark"] = "S"
                elif row["status"] == "timeout":
                    row["mark"] = "I"
                elif row["status"] != "ok":
                    row["mark"] = "X/H"
                elif not row["task_ok"]:
                    row["mark"] = "T"
                elif not row["tools_ok"] or not row["sample_sft"]:
                    row["mark"] = "D"
                elif row["sft_exclusion_reason"] or row["completion_status"] == "incomplete":
                    row["mark"] = "C"
                elif row["wire_status"] != "matched":
                    row["mark"] = "U"
                else:
                    row["mark"] = "PASS"
                rows.append(row)
    return rows


def report(root: Path, phase: str, token: str | None = None) -> dict:
    rows = result_rows(root, phase)
    summary = {
        "phase": phase, "expected": {"smoke": 7, "full": 196, "control": 28}[phase],
        "observed": len(rows), "runner_ok": sum(r["status"] == "ok" for r in rows),
        "task_ok": sum(r["task_ok"] for r in rows),
        "tools_ok": sum(r["tools_ok"] for r in rows),
        "wire_matched": sum(r["wire_status"] == "matched" for r in rows),
        "wire_mismatch": sum(r["wire_status"] == "mismatch" for r in rows),
        "wire_unverified": sum(r["wire_status"] == "unverified" for r in rows),
        "wire_not_observable": sum(r["wire_status"] == "not_observable" for r in rows),
        "native_request_audits": sum(r["wire_source"] == "native" for r in rows),
        "sft_excluded": sum(bool(r["sft_exclusion_reason"]) for r in rows),
        "marks": {mark: sum(r["mark"] == mark for r in rows)
                  for mark in ("PASS", "S", "U", "T", "D", "C", "I", "X/H")},
    }
    aggregate_rows = 0
    for harness in HARNESSES:
        for effort in EFFORTS:
            path = root / phase / f"{harness}-{effort}" / "sft.jsonl"
            if path.is_file():
                aggregate_rows += sum(bool(line.strip()) for line in path.open(encoding="utf-8"))
    summary["aggregate_sft_rows"] = aggregate_rows
    summary["expected_sft_rows"] = sum(r["status"] == "ok" and not r["sft_exclusion_reason"]
                                       and r["sample_sft"] for r in rows)
    summary["aggregate_consistent"] = aggregate_rows == summary["expected_sft_rows"]
    if token:
        needle = token.encode()
        summary["credential_leak_paths"] = [
            p.relative_to(root).as_posix() for p in (root / phase).rglob("*")
            if p.is_file() and contains_token(p, needle)
        ]
    (root / f"{phase}-results.json").write_text(
        json.dumps({"summary": summary, "cells": rows}, ensure_ascii=False, indent=2) + "\n"
    )
    return summary


def run_job(root: Path, phase: str, harness: str, effort: str, models: tuple[str, ...],
            api_base: str, token: str) -> dict:
    target = root / phase / f"{harness}-{effort}"
    command = [sys.executable, "-m", "roller", "collect",
               "--tasks", str(root / f"tasks-{effort}.jsonl"), "--harness", harness,
               "--api-base", api_base, "--api-key-env", "ROLLER_MATRIX_API_KEY",
               "--timeout", "180", "--parallel", "2", "--retries", "0", "--samples", "1",
               "--wire-trace", "full" if phase == "control" else "actf",
               "--output", str(root / phase), "--run-name", f"{harness}-{effort}"]
    if (target / "info.json").is_file():
        command.append("--resume")
    if harness == "openclaw":
        command.extend(["--model-config", str(root / "openclaw-models.json")])
    for model in models:
        command.extend(["--model", f"matrix/{model}" if harness == "openclaw" else model])
    env = os.environ.copy()
    env.update(ROLLER_MATRIX_API_KEY=token, ROLLER_MATRIX_BASE_URL=api_base,
               PYTHONPATH=str(ROOT / "src"))
    started = time.monotonic()
    log = root / phase / f"{harness}-{effort}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        process = subprocess.run(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, errors="replace",
                                 timeout=180 * math.ceil(len(models) / 2) + 300)
        output, code = process.stdout, process.returncode
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        code = 124
    log.write_text(output.replace(token, "[REDACTED]"))
    return {"phase": phase, "harness": harness, "effort": effort,
            "returncode": code, "seconds": round(time.monotonic() - started, 2)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("smoke", "full", "control", "report"))
    parser.add_argument("--api-base", required=False)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--run-root", type=Path,
                        default=ROOT / "runs" / "issue51-review-matrix-20260928")
    parser.add_argument("--report-phase", choices=("smoke", "full", "control"), default="full")
    args = parser.parse_args()
    root = args.run_root.resolve()
    if args.phase == "report":
        print(json.dumps(report(root, args.report_phase), indent=2))
        return 0
    if not args.api_base:
        parser.error("--api-base is required for live phases")
    if args.token_file is None or args.token_file.stat().st_mode & 0o077:
        parser.error("--token-file must be supplied with mode 0600")
    token = args.token_file.read_text().strip()
    if not token:
        parser.error("token file is empty")
    inputs(root)
    api_base = args.api_base.rstrip("/")
    models = (MODELS[0],) if args.phase in {"smoke", "control"} else MODELS
    efforts = ("low",) if args.phase == "smoke" else EFFORTS
    jobs = [(harness, effort) for harness in HARNESSES for effort in efforts]
    manifest = {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                                  text=True).strip(),
                "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
                "api_base": api_base, "models": models, "harnesses": HARNESSES,
                "efforts": efforts, "phase": args.phase, "timeout_seconds": 180,
                "retries": 0, "workers_per_job": 2,
                "concurrent_jobs": 4 if args.phase == "full" else 2}
    (root / f"{args.phase}-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    with ThreadPoolExecutor(max_workers=manifest["concurrent_jobs"]) as pool:
        futures = {pool.submit(run_job, root, args.phase, harness, effort, models,
                               api_base, token): (harness, effort) for harness, effort in jobs}
        for future in as_completed(futures):
            print(json.dumps(future.result()), flush=True)
            print(json.dumps(report(root, args.phase)), flush=True)
    summary = report(root, args.phase, token)
    print(json.dumps(summary, indent=2))
    return int(summary["observed"] != summary["expected"])


if __name__ == "__main__":
    raise SystemExit(main())
