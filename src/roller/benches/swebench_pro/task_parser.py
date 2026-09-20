"""Dataset discovery and field parsing for SWE-Bench Pro tasks."""

from __future__ import annotations

import ast
import importlib
import json
import shlex
from pathlib import Path, PurePosixPath
from typing import Any

from roller.benches.base import BenchFormatError


def _clean_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise BenchFormatError(f"{label}: must be a string")
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, str):
            text = decoded.strip()
    return text


def _string_list(value: Any, label: str) -> list[str]:
    if isinstance(value, list):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise BenchFormatError(f"{label}: invalid list literal") from exc
    else:
        raise BenchFormatError(f"{label}: must be a list or list literal")
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) for item in parsed
    ):
        raise BenchFormatError(f"{label}: must contain only strings")
    return parsed


def _test_files(value: Any, label: str) -> list[str]:
    command = _clean_text(value, label)
    lines = [line.strip() for line in command.splitlines() if line.strip()]
    if not lines:
        raise BenchFormatError(f"{label}: command is empty")
    try:
        tokens = shlex.split(lines[-1])
        separator = tokens.index("--")
    except (ValueError, IndexError) as exc:
        raise BenchFormatError(
            f"{label}: final command must be 'git checkout <commit> -- <paths>'"
        ) from exc
    if tokens[:2] != ["git", "checkout"] or separator < 3:
        raise BenchFormatError(
            f"{label}: final command must be 'git checkout <commit> -- <paths>'"
        )
    paths = tokens[separator + 1 :]
    if not paths:
        raise BenchFormatError(f"{label}: final checkout has no test paths")
    for path in paths:
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or not parsed.parts or ".." in parsed.parts:
            raise BenchFormatError(f"{label}: unsafe test path {path!r}")
    return paths


def _dataset_path(root: Path) -> Path:
    candidates = (
        root / "test.jsonl",
        root / "data.jsonl",
        root / "test.parquet",
        root / "data" / "test.parquet",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    names = ", ".join(str(path.relative_to(root)) for path in candidates)
    raise BenchFormatError(
        f"SWE-Bench Pro data not found under {root}; expected {names}"
    )


def _load_dataset(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as stream:
            for line_number, raw in enumerate(stream, start=1):
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise BenchFormatError(
                        f"{path}:{line_number}: invalid JSON: {exc.msg}"
                    ) from exc
                if not isinstance(row, dict):
                    raise BenchFormatError(
                        f"{path}:{line_number}: row must be an object"
                    )
                rows.append(row)
        return rows
    try:
        parquet = importlib.import_module("pyarrow.parquet")
    except ImportError as exc:
        raise BenchFormatError(
            "reading SWE-Bench Pro parquet requires `pip install -e "
            '".[swebench-pro]"`, or provide test.jsonl'
        ) from exc
    return parquet.read_table(path).to_pylist()
