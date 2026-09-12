#!/usr/bin/env python3
"""List, validate, run selected four-task groups, and check their saved results."""
from __future__ import annotations

import argparse
import copy
from dataclasses import replace
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from roller.benches.base import binding_from_task_row
from roller.bundles import TaskBundleStore
from roller.integrity import validate_resumable_sample
from roller.storage import safe_component
from roller.tasks import _read_rows, build_requests, load_tasks

HARNESSES = ("openclaw", "codex", "claude-code", "hermes", "opencode")
DEFAULT_MODELS = {"openclaw": "my-openai-proxy/my-model", "codex": "replace-codex-model", "claude-code": "replace-claude-model", "hermes": "replace-hermes-model", "opencode": "replace-opencode-model"}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def manifest() -> dict:
    return read_json(ROOT / "manifest.json")


def select(cases: list[dict], patterns: list[str] | None, exclude: list[str] | None = None) -> list[dict]:
    for pattern in [*(patterns or []), *(exclude or [])]:
        if not any(fnmatch.fnmatchcase(case["key"], pattern) for case in cases):
            raise ValueError(f"case pattern matched nothing: {pattern}")
    selected = [case for case in cases
                if (not patterns or any(fnmatch.fnmatchcase(case["key"], p) for p in patterns))
                and not any(fnmatch.fnmatchcase(case["key"], p) for p in (exclude or []))]
    if not selected:
        raise ValueError("case selection is empty after exclusions")
    return selected


def normalized(case: dict):
    if case.get("runtime_absolute_file_root"):
        # Keep committed paths portable while exercising the absolute-root parser.
        with tempfile.TemporaryDirectory(prefix="roller-coverage-absolute-") as temporary:
            path = Path(temporary) / (ROOT / case["path"]).name
            write_bound_list(path, bound_rows(case, DEFAULT_MODELS), case)
            return load_tasks(path, **case.get("parser_options", {}))
    return load_tasks(ROOT / case["path"], **case.get("parser_options", {}))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def absolute_sources(row: dict, original_dir: Path) -> dict:
    row = copy.deepcopy(row)
    # Moving a task list changes both explicit relative roots and the implicit root.
    for field in ("file_root", "workspace", "workspace_path"):
        value = row.get(field)
        if field == "file_root" and value is None:
            row[field] = str(original_dir)
        elif isinstance(value, str) and not Path(value).is_absolute():
            row[field] = str((original_dir / value).absolute())
    return row


def validate(cases: list[dict], negatives: list[dict]) -> None:
    sources = read_json(ROOT / "sources.json")
    for source in sources["files"]:
        path = ROOT / source["path"]
        check(hashlib.sha256(path.read_bytes()).hexdigest() == source["sha256"], f"copied source changed: {path}")
    count = 0
    with tempfile.TemporaryDirectory(prefix="roller-coverage-validate-") as temporary:
        root = Path(temporary)
        for number, case in enumerate(cases):
            tasks = normalized(case)
            check(len(tasks) == len(case["expectations"]) == case["count"] == 4, f"not four tasks: {case['key']}")
            bundles = TaskBundleStore(root / str(number)).prepare_all(tasks)
            models = ["validation-model"] if case["cli_model_required"] else None
            requests = build_requests(tasks, models, 180)
            check(len(requests) == 4, f"request count: {case['key']}")
            for task, expected in zip(tasks, case["expectations"]):
                if case.get("runtime_absolute_file_root"):
                    check(Path(task.raw["file_root"]).is_absolute(), f"expected runtime absolute root: {task.id}")
                check(task.id == expected["task_id"], f"task ID mismatch: {case['key']}")
                check(task.harness == case["harness"], f"Harness mismatch: {task.id}")
                if case["simulator"]:
                    check(task.user_simulator is not None, f"missing simulator: {task.id}")
                    check(task.user_simulator.harness == case["simulator"], f"simulator mismatch: {task.id}")
                    check(task.user_simulator.max_turns <= 2, f"too many turns: {task.id}")
                    check(not task.prompts, f"simulator/predefined conflict: {task.id}")
                else:
                    check(len(task.prompts) == expected["user_turns"], f"turn count: {task.id}")
                if "resume_safe" in expected:
                    check(bundles[task.id].resume_safe == expected["resume_safe"], f"capture policy: {task.id}")
                binding = binding_from_task_row(task.raw)
                if binding and binding.grade:
                    compile(binding.grade.code, task.id, "exec")
                for filename in expected.get("files", {}):
                    check(not Path(filename).is_absolute() and ".." not in Path(filename).parts, f"invalid expected output: {task.id}")
                count += 1
        negative_count = 0
        for group in negatives:
            path = ROOT / group["path"]
            entries = read_json(path)
            check(len(entries) == group["count"] == 4, f"not four negative cases: {path}")
            for i, entry in enumerate(entries):
                # A failing list stops at its first error; each negative row must be tested alone.
                row = absolute_sources(entry["task"], path.parent)
                task_path = root / "negative.jsonl"
                task_path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
                try:
                    tasks = load_tasks(task_path)
                    TaskBundleStore(root / "negative-bundle").prepare_all(tasks)
                except (ValueError, OSError) as exc:
                    check(entry["error_contains"] in str(exc), f"wrong rejection for {group['key']}[{i}]: {exc}")
                else:
                    raise ValueError(f"negative task unexpectedly accepted: {group['key']}[{i}]")
                negative_count += 1
    print(f"PASS: {len(cases)} positive groups / {count} tasks; {negative_count} independently rejected negative tasks; {len(sources['files'])} source checksums.")


def bound_rows(case: dict, models: dict[str, str]) -> list[dict]:
    path = ROOT / case["path"]
    rows = []
    for original in _read_rows(path):
        row = absolute_sources(original, path.parent)
        if "model" in row:
            row["model"] = models[case["harness"]]
        for field in ("agent", "task_agent"):
            if isinstance(row.get(field), dict) and "model" in row[field]:
                row[field]["model"] = models[case["harness"]]
        if isinstance(row.get("user_simulator"), dict) and "model" in row["user_simulator"]:
            row["user_simulator"]["model"] = models[case["simulator"]]
        rows.append(row)
    return rows


def write_bound_list(path: Path, rows: list[dict], case: dict) -> None:
    # Preserve the format under test even when binding local model names.
    scenario = case["scenario"]
    if scenario == "formats/json_array":
        text = json.dumps(rows, ensure_ascii=False)
    elif scenario == "formats/json_object":
        text = json.dumps({"tasks": rows}, ensure_ascii=False)
    else:
        text = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
        if scenario == "formats/jsonl_bom":
            text = "\ufeff\n" + text.replace("\n", "\n\n")
    path.write_text(text, encoding="utf-8")


def run_name(case: dict) -> str:
    return safe_component(case["key"].replace("/", "__"), max_length=100)


def run(cases: list[dict], args: argparse.Namespace, extra: list[str]) -> int:
    reserved = {"--tasks", "--model", "--harness", "--output", "--run-name", "--prompt-key", "--id-key", "--limit", "--start-index", "--workspace"}
    for value in extra:
        if value.split("=", 1)[0] in reserved:
            raise ValueError(f"{value} would change the selected matrix; use suite options or invoke roller directly")
    models = {name: getattr(args, name.replace("-", "_") + "_model") for name in HARNESSES}
    selected_harnesses = {c["harness"] for c in cases} | {c["simulator"] for c in cases if c["simulator"]}
    if not args.dry_run:
        for name in selected_harnesses:
            check(models[name] != DEFAULT_MODELS[name], f"set --{name}-model (or ROLLER_TEST_{name.upper().replace('-', '_')}_MODEL) to your actual model")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT / "src") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    failures = 0
    for case in cases:
        with tempfile.TemporaryDirectory(prefix="roller-coverage-run-") as temporary:
            source_path = ROOT / case["path"]
            path = Path(temporary) / ("tasks" + source_path.suffix)
            write_bound_list(path, bound_rows(case, models), case)
            command = [sys.executable, "-m", "roller", "collect", "--tasks", str(path), "--timeout", str(args.timeout), "--parallel", str(args.parallel), "--retries", "0", "--output", str(args.output.resolve()), "--run-name", run_name(case)]
            for key, value in case["parser_options"].items():
                command += ["--" + key.replace("_", "-"), value]
            if case["cli_model_required"]:
                command += ["--model", models[case["harness"]]]
            if args.dry_run:
                command.append("--dry-run")
            command += extra
            case_env = {**env, **case.get("environment", {})}
            print(f"[{case['key']}] 4 tasks; {'dry-run' if args.dry_run else 'collect'}", flush=True)
            result = subprocess.run(command, env=case_env, capture_output=args.dry_run, text=True)
            if result.returncode:
                failures += 1
                if args.dry_run:
                    print(result.stderr or result.stdout, file=sys.stderr)
            elif args.dry_run:
                plan = json.loads(result.stdout)
                check(plan["tasks"] == plan["trajectories"] == 4, f"dry-run count mismatch: {case['key']}")
                print("  PASS: 4 requests and file bundles validated")
    print(f"Finished {len(cases)} groups; {failures} failed.")
    return int(bool(failures))



def normalize_output(case_key: str, task, actual: str) -> str:
    # The directory prompt does not specify the relative-path base. Accept
    # either the workspace-relative or declared-directory-relative spelling.
    if case_key.startswith("inputs/directory/") and len(task.inputs) == 1:
        prefix = task.inputs[0].path.rstrip("/") + "/"
        lines = actual.splitlines()
        if lines and all(line.startswith(prefix) for line in lines):
            return "\n".join(line[len(prefix):] for line in lines)
    return actual


def trace_requirements(run_info: dict) -> dict[str, bool]:
    """Match the collector's trace policy, including mixed Harness runs."""
    configuration = run_info.get("configuration", {})
    configurations = {name: configuration[name] for name in HARNESSES if name in configuration}
    return {
        "require_wire_trace": any(config.get("wire_trace") == "full" for config in configurations.values()),
        "require_cache_trace": bool(configurations.get("openclaw", {}).get("cache_trace", False)),
    }


def check_results(cases: list[dict], output: Path, run_dir: Path | None) -> int:
    if run_dir is not None:
        check(len(cases) == 1, "--run-dir requires exactly one selected group")
    failures, checked = [], 0
    for case in cases:
        root = run_dir if run_dir is not None else output / run_name(case)
        try:
            run_info = read_json(root / "info.json")
            check(run_info.get("state") == "finished", f"run not finished: {root}")
            infos = sorted((root / "samples").glob("*/*/info.json"))
            check(len(infos) == 4, f"expected four samples: {root}")
            expectations = {item["task_id"]: item for item in case["expectations"]}
            tasks = {task.id: task for task in normalized(case)}
            sft_records = []
            seen = set()
            for info_path in infos:
                info = read_json(info_path)
                sample_dir = info_path.parent
                task_id = str(info["task_id"])
                check(task_id not in seen, f"duplicate task: {task_id}")
                seen.add(task_id)
                expected = expectations[task_id]
                check(info["status"] == expected["status"], f"{task_id}: status={info['status']}: {info.get('error')}")
                check(info["harness"] == case["harness"], f"wrong result Harness: {task_id}")
                request = build_requests([tasks[task_id]], [info["model"]], 180)[0]
                request = replace(request, index=info["request_index"])
                validate_resumable_sample(sample_dir, request, info, **trace_requirements(run_info))
                actf = read_json(sample_dir / "trace_ACTF.json")
                sft = read_json(sample_dir / "trace_SFT.json")
                check(set(sft) == {"messages", "tools"}, f"invalid SFT shape: {task_id}")
                sft_records.append((info["request_index"], sft))
                users = [m for m in sft["messages"] if m["role"] == "user"]
                check(len(users) == expected["user_turns"], f"wrong user turn count: {task_id}")
                if "reply" in expected:
                    replies = [m["content"] for m in sft["messages"] if m["role"] == "assistant" and m.get("content")]
                    check(bool(replies) and replies[-1].strip() == expected["reply"], f"unexpected final reply: {task_id}")
                calls = sum(len(step.get("assistant_content", {}).get("tool_calls", [])) for step in actf["steps"])
                check(calls >= expected.get("tool_calls_min", 0), f"missing tool call: {task_id}")
                for name, content in expected.get("files", {}).items():
                    check(normalize_output(case["key"], tasks[task_id], (sample_dir / "results" / name).read_text().strip()) == content, f"wrong output {name}: {task_id}")
                if expected.get("no_output_files"):
                    check(not any(p.is_file() for p in (sample_dir / "results").rglob("*")), f"unexpected output: {task_id}")
                if "resume_safe" in expected:
                    check(info["provenance"]["resume_safe"] == expected["resume_safe"], f"wrong capture policy: {task_id}")
                if "rounds" in expected:
                    conversation = read_json(sample_dir / "conversation.json")
                    check(conversation["participants"]["task_agent"]["harness"] == case["harness"], f"wrong conversation agent: {task_id}")
                    check(conversation["participants"]["user_simulator"]["harness"] == case["simulator"], f"wrong conversation simulator: {task_id}")
                    check(len(conversation["rounds"]) == expected["rounds"], f"wrong simulator round count: {task_id}")
                    check(conversation["termination"]["reason"] == expected["termination"], f"wrong simulator termination: {task_id}")
                if "score" in expected:
                    check(read_json(sample_dir / "score.json")["scores"]["overall_score"] == expected["score"], f"grader score: {task_id}")
                checked += 1
            check(seen == set(expectations), f"missing tasks: {root}")
            aggregate = [json.loads(line) for line in (root / "sft.jsonl").read_text().splitlines() if line.strip()]
            check(aggregate == [sft for _, sft in sorted(sft_records)], f"SFT aggregation or order mismatch: {root}")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            failures.append(f"{case['key']}: {exc}")
    for failure in failures:
        print("FAIL: " + failure)
    print(f"Checked {checked} samples; {len(failures)} groups failed.")
    return int(bool(failures))


def main() -> int:
    tokens = sys.argv[1:]
    split = tokens.index("--") if "--" in tokens else len(tokens)
    extra = tokens[split + 1:] if split < len(tokens) else []
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("list", "validate", "run", "check"):
        sub = commands.add_parser(command)
        sub.add_argument("--case", action="append", required=command in {"run", "check"}, help="case key or quoted glob; repeatable")
        sub.add_argument("--exclude-case", action="append", help="exclude a case key or quoted glob; repeatable")
        if command in {"run", "check"}:
            sub.add_argument("--output", type=Path, default=PROJECT / "runs/coverage")
        if command == "check":
            sub.add_argument("--run-dir", type=Path)
        if command == "run":
            sub.add_argument("--dry-run", action="store_true")
            sub.add_argument("--parallel", type=int, default=2)
            sub.add_argument("--timeout", type=int, default=180)
            for harness in HARNESSES:
                variable = "ROLLER_TEST_" + harness.upper().replace("-", "_") + "_MODEL"
                sub.add_argument("--" + harness + "-model", default=os.environ.get(variable, DEFAULT_MODELS[harness]))
    args = parser.parse_args(tokens[:split])
    if extra and args.command != "run":
        parser.error("arguments after -- are only supported by run")
    data = manifest()
    cases = select(data["cases"], args.case, args.exclude_case)
    if args.command == "list":
        for case in cases:
            print(f"{case['key']:<52} {case['count']}  {case['tier']:<10} {case['description']}")
        print(f"{len(cases)} positive groups / {sum(c['count'] for c in cases)} tasks; {data['negative_tasks']} negative cases available through validate.")
    elif args.command == "validate":
        validate(cases, data["negative"])
    elif args.command == "run":
        return run(cases, args, extra)
    else:
        return check_results(cases, args.output, args.run_dir)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
