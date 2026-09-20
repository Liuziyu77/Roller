import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath

_BASELINE_REF = "refs/roller/baseline"
_GRADE_DIR = ".roller-grade"
_OUTPUT_DIR = ".roller_outputs"


def _run(command, workspace, timeout=None):
    return subprocess.run(
        command,
        cwd=workspace,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )


def _detail(result):
    return (result.stderr or result.stdout or "").strip()[-4000:]


def _safe_relative(value):
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("unsafe test path: %r" % value)
    return path


def _remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        os.symlink(os.readlink(source), destination)
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    elif source.is_file():
        shutil.copy2(source, destination, follow_symlinks=False)


def _strip_binary_hunks(patch):
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    kept = []
    for section in sections:
        if not section.strip():
            continue
        if re.search(r"^Binary files .* differ$", section, re.MULTILINE):
            continue
        if re.search(r"^GIT binary patch$", section, re.MULTILINE):
            continue
        kept.append(section)
    return "".join(kept)


def _error(message, **details):
    payload = {"overall_score": 0.0, "resolved": 0, "error": message}
    payload.update(details)
    return payload


def _without_agent_tests(patch):
    kept, excluded = [], []
    for section in re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE):
        if not section.strip():
            continue
        header = shlex.split(section.splitlines()[0])
        paths = [item[2:] for item in header[2:4]]
        def is_test(path):
            parsed = PurePosixPath(path)
            name = parsed.name
            test_directories = {"test", "tests", "__tests__", "spec", "specs"}
            source_suffixes = {".go", ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
            return (
                name.endswith("_test.go")
                or name.startswith("test_") and name.endswith(".py")
                or name.endswith("_test.py")
                or re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", name) is not None
                or bool(test_directories.intersection(parsed.parts[:-1]))
                and parsed.suffix.lower() in source_suffixes
            )
        if paths and all(is_test(path) for path in paths):
            excluded.extend(paths)
        else:
            kept.append(section)
    return "".join(kept), sorted(set(excluded))


def roller_grade(
    workspace_path,
    fail_to_pass,
    pass_to_pass,
    selected_test_files,
    test_files,
    test_timeout_seconds=3300,
    exclude_agent_tests=False,
    supplemental_test_fixtures=None,
    prepare_go_dependencies=False,
):
    workspace = Path(workspace_path)
    grade_dir = workspace / _GRADE_DIR
    output_dir = workspace / _OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    model_patch_path = output_dir / "model.patch"
    stdout_path = output_dir / "test_stdout.log"
    stderr_path = output_dir / "test_stderr.log"
    parsed_path = output_dir / "test_output.json"
    test_patch_path = grade_dir / "test.patch"
    run_script_path = grade_dir / "run_script.sh"
    parser_path = grade_dir / "parser.py"

    try:
        for path in (test_patch_path, run_script_path, parser_path):
            if not path.is_file():
                return _error("missing staged grader file: %s" % path.name)

        intent = _run(["git", "add", "-N", "--", "."], workspace)
        if intent.returncode != 0:
            return _error("could not enumerate model changes: %s" % _detail(intent))
        diff = _run(
            [
                "git",
                "diff",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                _BASELINE_REF,
                "--",
                ".",
            ],
            workspace,
        )
        if diff.returncode != 0:
            return _error("could not extract model patch: %s" % _detail(diff))
        model_patch = _strip_binary_hunks(diff.stdout)
        model_patch_path.write_text(model_patch, encoding="utf-8")
        grading_patch = model_patch
        excluded = []
        if exclude_agent_tests:
            grading_patch, excluded = _without_agent_tests(model_patch)
        grading_patch_path = output_dir / "graded.patch"
        grading_patch_path.write_text(grading_patch, encoding="utf-8")

        reset = _run(["git", "reset", "--hard", _BASELINE_REF], workspace)
        if reset.returncode != 0:
            return _error("could not restore benchmark baseline: %s" % _detail(reset))
        clean = _run(["git", "clean", "-fd"], workspace)
        if clean.returncode != 0:
            return _error("could not clean benchmark baseline: %s" % _detail(clean))

        hidden = _run(["git", "apply", "--verbose", str(test_patch_path)], workspace)
        if hidden.returncode != 0:
            return _error("hidden test patch did not apply: %s" % _detail(hidden))
        if supplemental_test_fixtures:
            fixtures = _run(
                ["git", "apply", "--verbose", str(grade_dir / "test-fixtures.patch")],
                workspace,
            )
            if fixtures.returncode != 0:
                return _error("test fixture patch did not apply: %s" % _detail(fixtures))
        hidden_intent = _run(["git", "add", "-N", "--", "."], workspace)
        if hidden_intent.returncode != 0:
            return _error("could not enumerate hidden tests: %s" % _detail(hidden_intent))
        changed = _run(
            [
                "git",
                "-c",
                "core.quotepath=false",
                "diff",
                "--name-only",
                "-z",
                _BASELINE_REF,
                "--",
                ".",
            ],
            workspace,
        )
        if changed.returncode != 0:
            return _error("could not enumerate hidden test files: %s" % _detail(changed))
        hidden_paths = list(test_files)
        hidden_paths.extend(item for item in changed.stdout.split("\0") if item)
        hidden_paths = list(dict.fromkeys(hidden_paths))

        with tempfile.TemporaryDirectory(prefix="roller-swebench-tests-") as temporary:
            snapshot = Path(temporary)
            present = set()
            for index, relative in enumerate(hidden_paths):
                relative_path = _safe_relative(relative)
                source = workspace.joinpath(*relative_path.parts)
                if source.exists() or source.is_symlink():
                    _copy(source, snapshot / str(index))
                    present.add(index)

            for relative in hidden_paths:
                relative_path = _safe_relative(relative)
                _remove(workspace.joinpath(*relative_path.parts))
            reset = _run(["git", "reset", "--hard", _BASELINE_REF], workspace)
            if reset.returncode != 0:
                return _error("could not reset after staging hidden tests: %s" % _detail(reset))
            clean = _run(["git", "clean", "-fd"], workspace)
            if clean.returncode != 0:
                return _error("could not clean after staging hidden tests: %s" % _detail(clean))

            if grading_patch.strip():
                applied = _run(["git", "apply", "--verbose", str(grading_patch_path)], workspace)
                if applied.returncode != 0:
                    return _error(
                        "model patch did not apply to a clean instance: %s" % _detail(applied)
                    )

            for index, relative in enumerate(hidden_paths):
                relative_path = _safe_relative(relative)
                destination = workspace.joinpath(*relative_path.parts)
                _remove(destination)
                if index in present:
                    _copy(snapshot / str(index), destination)

        test_started = time.monotonic()
        dependency_preparation = None
        selected_argument = ",".join(selected_test_files)
        for test_attempt in range(2):
            remaining = max(1, test_timeout_seconds - int(time.monotonic() - test_started))
            with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr_handle:
                try:
                    test_result = subprocess.run(
                        ["bash", str(run_script_path), selected_argument],
                        cwd=workspace, stdout=stdout_handle, stderr=stderr_handle,
                        text=True, timeout=remaining,
                    )
                except subprocess.TimeoutExpired:
                    return _error(
                        "official test script timed out",
                        test_timeout_seconds=test_timeout_seconds,
                    )
            test_output = stdout_path.read_text(errors="replace")
            test_output += stderr_path.read_text(errors="replace")
            missing_sums = "missing go.sum entry" in test_output
            if not (test_attempt == 0 and prepare_go_dependencies and missing_sums
                    and (workspace / "go.mod").is_file()):
                break
            # Keep the original failed run and record every dependency-only edit.
            shutil.copy2(stdout_path, output_dir / "before-dependency-stdout.log")
            shutil.copy2(stderr_path, output_dir / "before-dependency-stderr.log")
            before = {
                name: (workspace / name).read_text() if (workspace / name).exists() else ""
                for name in ("go.mod", "go.sum")
            }
            remaining = max(1, test_timeout_seconds - int(time.monotonic() - test_started))
            with (output_dir / "dependency-preparation.log").open("w") as handle:
                try:
                    prepared = subprocess.run(
                        ["go", "mod", "download", "all"], cwd=workspace,
                        stdout=handle, stderr=subprocess.STDOUT, timeout=min(600, remaining),
                    )
                    dependency_preparation = {"returncode": prepared.returncode}
                except subprocess.TimeoutExpired:
                    dependency_preparation = {"error": "go mod download timed out"}
            changes = []
            for name, previous in before.items():
                current = (workspace / name).read_text() if (workspace / name).exists() else ""
                changes.extend(difflib.unified_diff(
                    previous.splitlines(True), current.splitlines(True),
                    fromfile="a/" + name, tofile="b/" + name,
                ))
            (output_dir / "dependency-preparation.patch").write_text("".join(changes))
            dependency_preparation["patch_bytes"] = len("".join(changes).encode())
            if dependency_preparation.get("returncode") != 0:
                return _error(
                    "Go dependency preparation failed",
                    dependency_preparation=dependency_preparation,
                )

        parsed = _run(
            [
                sys.executable,
                str(parser_path),
                str(stdout_path),
                str(stderr_path),
                str(parsed_path),
            ],
            workspace,
            timeout=120,
        )
        if parsed.returncode != 0 or not parsed_path.is_file():
            return _error(
                "official result parser failed: %s" % _detail(parsed),
                test_returncode=test_result.returncode,
            )
        try:
            output = json.loads(parsed_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return _error(
                "official result parser produced invalid JSON: %s" % exc,
                test_returncode=test_result.returncode,
            )
        tests = output.get("tests") if isinstance(output, dict) else None
        if not isinstance(tests, list):
            return _error(
                "official result parser produced no tests list",
                test_returncode=test_result.returncode,
            )
        passed = {
            item.get("name")
            for item in tests
            if isinstance(item, dict)
            and item.get("status") == "PASSED"
            and isinstance(item.get("name"), str)
        }
        required = set(fail_to_pass) | set(pass_to_pass)
        missing = sorted(required - passed)
        resolved = not missing
        observed = {
            item.get("name") for item in tests
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        failed = {
            item.get("name") for item in tests
            if isinstance(item, dict) and item.get("status") in ("FAILED", "ERROR")
            and isinstance(item.get("name"), str)
        }
        return {
            "overall_score": float(resolved),
            "resolved": int(resolved),
            "test_returncode": test_result.returncode,
            "tests_seen": len(tests),
            "required_tests": len(required),
            "passed_required_tests": len(required) - len(missing),
            "fail_to_pass_tests": len(fail_to_pass),
            "pass_to_pass_tests": len(pass_to_pass),
            "missing_tests": missing,
            "failed_required_tests": sorted(required & failed),
            "unobserved_required_tests": sorted(required - observed),
            "passed_fail_to_pass_tests": len(set(fail_to_pass) & passed),
            "passed_pass_to_pass_tests": len(set(pass_to_pass) & passed),
            "excluded_agent_test_files": excluded,
            "supplemental_test_fixtures": list(supplemental_test_fixtures or []),
            "dependency_preparation": dependency_preparation,
            "model_patch_bytes": len(model_patch.encode("utf-8")),
            "graded_patch_bytes": len(grading_patch.encode("utf-8")),
        }
    except Exception as exc:
        return _error("SWE-Bench Pro grader failed: %s: %s" % (type(exc).__name__, exc))
