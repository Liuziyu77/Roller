"""QwenClawBench workspace materialization and path compatibility."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from roller.benches.base import BenchFormatError
from roller.benches.qwenclaw.task_parser import QwenClawTask


def _relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BenchFormatError(f"{label}: must be a non-empty relative path")
    raw = value.strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise BenchFormatError(f"{label}: unsafe path {value!r}")
    return path.as_posix()


def _source_path(assets_root: Path, task_id: str, source: str) -> Path:
    relative = _relative_path(source, f"{task_id}.workspace_files.source")
    candidates = (assets_root / task_id / relative, assets_root / relative)
    for candidate in candidates:
        if not candidate.exists():
            continue
        cursor = assets_root
        for part in candidate.relative_to(assets_root).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise BenchFormatError(
                    f"{task_id}: workspace source is a symlink: {cursor}"
                )
        resolved = candidate.resolve()
        if not resolved.is_relative_to(assets_root.resolve()):
            raise BenchFormatError(
                f"{task_id}: workspace source escapes assets: {source}"
            )
        return candidate
    searched = ", ".join(str(path) for path in candidates)
    raise BenchFormatError(
        f"{task_id}: missing workspace source {source!r}; searched {searched}"
    )


def _hash_source(digest: Any, source: Path, logical: str) -> None:
    if source.is_dir():
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise BenchFormatError(f"workspace source contains a symlink: {path}")
            relative = path.relative_to(source).as_posix()
            mode = path.stat().st_mode
            if path.is_dir():
                digest.update(f"D:{logical}/{relative}\0".encode())
            elif path.is_file() and stat.S_ISREG(mode):
                digest.update(f"F:{logical}/{relative}\0".encode())
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            else:
                raise BenchFormatError(
                    f"workspace source is not a regular file: {path}"
                )
        return
    mode = source.stat().st_mode
    if not stat.S_ISREG(mode):
        raise BenchFormatError(f"workspace source is not a regular file: {source}")
    digest.update(f"F:{logical}\0".encode())
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)


def _cache_root() -> Path:
    configured = (os.environ.get("QWENCLAW_CACHE_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    xdg = (os.environ.get("XDG_CACHE_HOME") or "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return (base / "roller" / "benches" / "qwenclawbench").resolve()


def materialize_workspace(
    task: QwenClawTask, assets_root: Path, cache_root: Path | None = None
) -> Path:
    """Build the exact source-to-destination workspace expected upstream."""

    entries: list[tuple[str, Path | None, str | None]] = []
    destinations: list[str] = []
    digest = hashlib.sha256()
    digest.update(task.source_path.read_bytes())
    for index, item in enumerate(task.workspace_files):
        destination_value = item.get("dest", item.get("path"))
        destination = _relative_path(
            destination_value, f"{task.task_id}.workspace_files[{index}].dest"
        )
        for existing in destinations:
            if (
                destination == existing
                or destination.startswith(existing + "/")
                or existing.startswith(destination + "/")
            ):
                raise BenchFormatError(
                    f"{task.task_id}: colliding workspace destinations "
                    f"{existing!r} and {destination!r}"
                )
        destinations.append(destination)
        if "content" in item:
            content = item["content"]
            if not isinstance(content, str):
                raise BenchFormatError(
                    f"{task.task_id}.workspace_files[{index}].content must be a string"
                )
            digest.update(f"C:{destination}\0{content}".encode())
            entries.append((destination, None, content))
            continue
        source_value = item.get("source")
        if not isinstance(source_value, str):
            raise BenchFormatError(
                f"{task.task_id}.workspace_files[{index}].source must be a string"
            )
        source = _source_path(assets_root, task.task_id, source_value)
        digest.update(f"M:{source_value}->{destination}\0".encode())
        _hash_source(digest, source, destination)
        entries.append((destination, source, None))

    fingerprint = digest.hexdigest()
    task_cache = (cache_root or _cache_root()) / task.task_id
    target = task_cache / fingerprint
    workspace = target / "workspace"
    if (target / "manifest.json").is_file() and workspace.is_dir():
        return workspace

    task_cache.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{fingerprint}.", dir=task_cache))
    temporary_workspace = temporary / "workspace"
    temporary_workspace.mkdir()
    try:
        for destination, source, content in entries:
            output = temporary_workspace / destination
            output.parent.mkdir(parents=True, exist_ok=True)
            if content is not None:
                output.write_text(content, encoding="utf-8")
            elif source is not None and source.is_dir():
                shutil.copytree(source, output, copy_function=shutil.copy2)
            elif source is not None:
                shutil.copy2(source, output)
        (temporary / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "roller.qwenclaw_workspace/v1",
                    "task_id": task.task_id,
                    "fingerprint": fingerprint,
                    "source": str(task.source_path),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            temporary.rename(target)
        except FileExistsError:
            shutil.rmtree(temporary)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return workspace


def workspace_compatibility_script(workspace: str) -> str:
    """Make ``workspace/...`` and workspace-relative paths refer to one tree."""

    root = workspace.rstrip("/")
    alias = f"{root}/workspace"
    quoted_root = shlex.quote(root)
    quoted_alias = shlex.quote(alias)
    return (
        "set -eu; "
        f"if [ -L {quoted_alias} ]; then "
        f'test "$(readlink -f {quoted_alias})" = "$(readlink -f {quoted_root})"; '
        f"elif [ -d {quoted_alias} ]; then "
        f"cp -a {quoted_alias}/. {quoted_root}/; "
        f"rm -rf {quoted_alias}; "
        f"ln -s . {quoted_alias}; "
        f"elif [ -e {quoted_alias} ]; then "
        "echo 'workspace compatibility path is not a directory' >&2; exit 1; "
        f"else ln -s . {quoted_alias}; fi"
    )
