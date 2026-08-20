from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from roller.models import ContextFile, InputFile, RunRequest, Task, TaskBundle
from roller.storage import atomic_write_json, safe_component


class BundleError(ValueError):
    pass


@dataclass(frozen=True)
class BundlePolicy:
    max_file_bytes: int = 10 * 1024 * 1024 * 1024
    max_task_input_bytes: int = 50 * 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_file_bytes <= 0 or self.max_task_input_bytes <= 0:
            raise ValueError("bundle size limits must be positive")


@dataclass(frozen=True)
class _ScannedFile:
    relative_path: str
    source: Path
    sha256: str
    size: int
    mode: int


@dataclass(frozen=True)
class _ScannedSource:
    files: tuple[_ScannedFile, ...]
    directories: tuple[str, ...]
    digest: str
    total_bytes: int


def _canonical_digest(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _join_target(target: str, relative_path: str) -> str:
    parts: list[str] = [] if target == "." else list(PurePosixPath(target).parts)
    if relative_path and relative_path != ".":
        parts.extend(PurePosixPath(relative_path).parts)
    return PurePosixPath(*parts).as_posix() if parts else "."


class TaskBundleStore:
    """Build immutable task snapshots and reusable content-addressed trees."""

    def __init__(self, run_dir: Path, policy: BundlePolicy | None = None) -> None:
        self.run_dir = run_dir.expanduser().resolve()
        self.policy = policy or BundlePolicy()
        self.blobs_root = self.run_dir / "blobs" / "sha256"
        self.trees_root = self.run_dir / "trees" / "sha256"
        self.tasks_root = self.run_dir / "tasks"
        self.runtime_root = self.run_dir / ".runtime"
        self._scan_cache: dict[Path, _ScannedSource] = {}
        for path in (self.blobs_root, self.trees_root, self.tasks_root, self.runtime_root):
            path.mkdir(parents=True, exist_ok=True)
        try:
            self.runtime_root.chmod(0o700)
        except OSError:
            pass

    def _scan_source(self, source: Path, required: bool, label: str) -> _ScannedSource | None:
        if not source.exists():
            if required:
                raise BundleError(f"{label} does not exist: {source}")
            return None
        if source.is_symlink():
            raise BundleError(f"{label} must not be a symbolic link: {source}")
        cached = self._scan_cache.get(source)
        if cached is not None:
            return cached

        files: list[_ScannedFile] = []
        directories: list[str] = []
        total_bytes = 0

        def add_file(path: Path, relative_path: str) -> None:
            nonlocal total_bytes
            if path.is_symlink():
                raise BundleError(f"{label} contains a symbolic link: {path}")
            if not path.is_file():
                raise BundleError(f"{label} contains a non-regular file: {path}")
            digest, size = _hash_file(path)
            if size > self.policy.max_file_bytes:
                raise BundleError(
                    f"{label} file exceeds max_file_bytes ({size} > "
                    f"{self.policy.max_file_bytes}): {path}"
                )
            total_bytes += size
            if total_bytes > self.policy.max_task_input_bytes:
                raise BundleError(
                    f"{label} exceeds max_task_input_bytes ({total_bytes} > "
                    f"{self.policy.max_task_input_bytes})"
                )
            files.append(
                _ScannedFile(
                    relative_path=relative_path,
                    source=path,
                    sha256=digest,
                    size=size,
                    mode=stat.S_IMODE(path.stat().st_mode),
                )
            )

        if source.is_file():
            add_file(source, ".")
        elif source.is_dir():
            for current, dir_names, file_names in os.walk(source, followlinks=False):
                current_path = Path(current)
                dir_names.sort()
                file_names.sort()
                for name in dir_names:
                    directory = current_path / name
                    if directory.is_symlink():
                        raise BundleError(f"{label} contains a symbolic-link directory: {directory}")
                    relative = directory.relative_to(source).as_posix()
                    directories.append(relative)
                for name in file_names:
                    file_path = current_path / name
                    add_file(file_path, file_path.relative_to(source).as_posix())
        else:
            raise BundleError(f"{label} must be a regular file or directory: {source}")

        digest = _canonical_digest(
            {
                "directories": sorted(directories),
                "files": [
                    {
                        "path": item.relative_path,
                        "sha256": item.sha256,
                        "size": item.size,
                        "mode": item.mode,
                    }
                    for item in files
                ],
            }
        )
        scanned = _ScannedSource(tuple(files), tuple(sorted(directories)), digest, total_bytes)
        self._scan_cache[source] = scanned
        return scanned

    def _store_blob(self, item: _ScannedFile) -> Path:
        destination = self.blobs_root / item.sha256
        if destination.is_file():
            return destination
        fd, temporary_name = tempfile.mkstemp(prefix=f".{item.sha256}.", dir=self.blobs_root)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            shutil.copyfile(item.source, temporary)
            copied_digest, copied_size = _hash_file(temporary)
            if copied_digest != item.sha256 or copied_size != item.size:
                raise BundleError(f"source changed while snapshotting: {item.source}")
            temporary.chmod(0o444)
            try:
                os.replace(temporary, destination)
            except FileExistsError:
                temporary.unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    @staticmethod
    def _add_directory(
        target: str, files: set[str], directories: set[str], label: str
    ) -> None:
        if target == ".":
            return
        if target in files or any(target.startswith(f"{existing}/") for existing in files):
            raise BundleError(f"{label} file/directory collision at: {target}")
        directories.add(target)

    @staticmethod
    def _ensure_available(
        target: str, files: set[str], directories: set[str], label: str
    ) -> None:
        if target in files or target in directories:
            raise BundleError(f"{label} target collision: {target}")
        prefix = f"{target}/"
        if any(existing.startswith(prefix) or target.startswith(f"{existing}/") for existing in files):
            raise BundleError(f"{label} file/directory collision at: {target}")
        if any(existing.startswith(prefix) for existing in directories):
            raise BundleError(f"{label} file/directory collision at: {target}")
        files.add(target)

    def _prepare_inputs(self, task: Task) -> tuple[Path, dict[str, Any]]:
        file_records: list[dict[str, Any]] = []
        directories: set[str] = set()
        occupied_files: set[str] = set()
        declarations: list[dict[str, Any]] = []
        total_bytes = 0

        for index, spec in enumerate(task.inputs):
            scanned = self._scan_source(spec.source, spec.required, f"task {task.id} input {index}")
            if scanned is None:
                declarations.append(
                    {"path": spec.path, "required": spec.required, "present": False}
                )
                continue
            actual_source_digest = (
                scanned.files[0].sha256 if spec.source.is_file() else scanned.digest
            )
            if spec.expected_sha256 and actual_source_digest != spec.expected_sha256:
                raise BundleError(
                    f"task {task.id} input {index} sha256 mismatch: "
                    f"expected {spec.expected_sha256}, got {actual_source_digest}"
                )
            declarations.append(
                {
                    "path": spec.path,
                    "required": spec.required,
                    "present": True,
                    "source_digest": actual_source_digest,
                }
            )
            total_bytes += scanned.total_bytes
            if total_bytes > self.policy.max_task_input_bytes:
                raise BundleError(
                    f"task {task.id} inputs exceed max_task_input_bytes "
                    f"({total_bytes} > {self.policy.max_task_input_bytes})"
                )
            for directory in scanned.directories:
                self._add_directory(
                    _join_target(spec.path, directory),
                    occupied_files,
                    directories,
                    f"task {task.id} input",
                )
            if spec.source.is_dir() and spec.path != ".":
                self._add_directory(
                    spec.path, occupied_files, directories, f"task {task.id} input"
                )
            for item in scanned.files:
                logical_path = (
                    spec.path
                    if item.relative_path == "."
                    else _join_target(spec.path, item.relative_path)
                )
                self._ensure_available(
                    logical_path,
                    occupied_files,
                    directories,
                    f"task {task.id} input",
                )
                blob = self._store_blob(item)
                file_records.append(
                    {
                        "logical_path": logical_path,
                        "sha256": item.sha256,
                        "size": item.size,
                        "mode": item.mode,
                        "blob": blob.relative_to(self.run_dir).as_posix(),
                    }
                )

        file_records.sort(key=lambda row: row["logical_path"])
        tree_digest = _canonical_digest(
            {"directories": sorted(directories), "files": file_records}
        )
        tree_root = self.trees_root / tree_digest
        if not tree_root.exists():
            temporary_root = Path(
                tempfile.mkdtemp(prefix=f".{tree_digest}.", dir=self.trees_root)
            )
            try:
                for directory in sorted(directories):
                    if directory != ".":
                        (temporary_root / directory).mkdir(parents=True, exist_ok=True)
                for record in file_records:
                    destination = temporary_root / record["logical_path"]
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(self.run_dir / record["blob"], destination)
                    destination.chmod(record["mode"])
                try:
                    temporary_root.rename(tree_root)
                except FileExistsError:
                    shutil.rmtree(temporary_root)
            finally:
                if temporary_root.exists():
                    shutil.rmtree(temporary_root)
        manifest = {
            "schema_version": "roller.inputs/v1",
            "tree_digest": tree_digest,
            "total_bytes": total_bytes,
            "declarations": declarations,
            "directories": sorted(directories),
            "files": file_records,
        }
        return tree_root, manifest

    def _prepare_context(self, task: Task, task_key: str) -> tuple[Path, dict[str, Any]]:
        context_root = self.runtime_root / "context" / task_key
        if context_root.exists():
            shutil.rmtree(context_root)
        context_root.mkdir(parents=True, exist_ok=True)
        occupied_files: set[str] = set()
        occupied_directories: set[str] = set()
        declarations: list[dict[str, Any]] = []

        for index, spec in enumerate(task.context_files):
            scanned = self._scan_source(
                spec.source, spec.required, f"task {task.id} context {index}"
            )
            if scanned is None:
                declarations.append(
                    {
                        "role": spec.role,
                        "target": spec.target,
                        "capture": spec.capture,
                        "required": spec.required,
                        "present": False,
                    }
                )
                continue
            declaration: dict[str, Any] = {
                "role": spec.role,
                "target": spec.target,
                "capture": spec.capture,
                "required": spec.required,
                "present": True,
                "source_type": "directory" if spec.source.is_dir() else "file",
                "directories": [],
                "files": [],
            }
            for directory in scanned.directories:
                target_directory = _join_target(spec.target, directory)
                if target_directory != ".":
                    if PurePosixPath(target_directory).parts[0] in {
                        "inputs",
                        "outputs",
                        "context",
                    }:
                        raise BundleError(
                            f"task {task.id} context target uses reserved top-level path: "
                            f"{target_directory}"
                        )
                    self._add_directory(
                        target_directory,
                        occupied_files,
                        occupied_directories,
                        f"task {task.id} context",
                    )
                    (context_root / target_directory).mkdir(parents=True, exist_ok=True)
                    declaration["directories"].append(target_directory)
            if spec.source.is_dir() and spec.target != ".":
                self._add_directory(
                    spec.target,
                    occupied_files,
                    occupied_directories,
                    f"task {task.id} context",
                )
                (context_root / spec.target).mkdir(parents=True, exist_ok=True)
                declaration["directories"].append(spec.target)
            for item in scanned.files:
                target = spec.target if item.relative_path == "." else _join_target(
                    spec.target, item.relative_path
                )
                if PurePosixPath(target).parts[0] in {"inputs", "outputs", "context"}:
                    raise BundleError(
                        f"task {task.id} context target uses reserved top-level path: {target}"
                    )
                self._ensure_available(
                    target,
                    occupied_files,
                    occupied_directories,
                    f"task {task.id} context",
                )
                destination = context_root / target
                destination.parent.mkdir(parents=True, exist_ok=True)
                if spec.capture == "full":
                    source_for_copy = self._store_blob(item)
                else:
                    source_for_copy = item.source
                shutil.copyfile(source_for_copy, destination)
                copied_digest, copied_size = _hash_file(destination)
                if copied_digest != item.sha256 or copied_size != item.size:
                    raise BundleError(f"context changed while snapshotting: {item.source}")
                destination.chmod(item.mode)
                file_record: dict[str, Any] = {
                    "logical_path": target,
                    "size": item.size,
                    "mode": item.mode,
                }
                if spec.capture in {"full", "hash"}:
                    file_record["sha256"] = item.sha256
                if spec.capture == "full":
                    file_record["blob"] = (
                        self.blobs_root / item.sha256
                    ).relative_to(self.run_dir).as_posix()
                declaration["files"].append(file_record)
            declaration["files"].sort(key=lambda row: row["logical_path"])
            declaration["directories"] = sorted(set(declaration["directories"]))
            declarations.append(declaration)

        manifest = {
            "schema_version": "roller.context/v1",
            "resume_safe": all(
                declaration.get("capture") != "none" or not declaration.get("present")
                for declaration in declarations
            ),
            "declarations": declarations,
        }
        return context_root, manifest

    def prepare(self, task: Task) -> TaskBundle:
        task_key = f"{task.source_index:08d}_{safe_component(task.id)}"
        task_root = self.tasks_root / task_key
        task_root.mkdir(parents=True, exist_ok=True)
        inputs_root, input_manifest = self._prepare_inputs(task)
        context_root, context_manifest = self._prepare_context(task, task_key)
        input_manifest_path = task_root / "inputs.manifest.json"
        context_manifest_path = task_root / "context.manifest.json"
        atomic_write_json(input_manifest_path, input_manifest)
        atomic_write_json(context_manifest_path, context_manifest)
        input_manifest_digest = _canonical_digest(input_manifest)
        context_manifest_digest = _canonical_digest(context_manifest)

        manifest = {
            "schema_version": task.schema_version,
            "id": task.id,
            "source_index": task.source_index,
            "messages": [{"role": "user", "content": prompt} for prompt in task.prompts],
            "model": task.model,
            "files": {
                "inputs_manifest": input_manifest_path.name,
                "inputs_tree_digest": input_manifest["tree_digest"],
                "inputs_manifest_digest": input_manifest_digest,
                "context_manifest": context_manifest_path.name,
                "context_manifest_digest": context_manifest_digest,
                "output_dir": task.output_dir,
                "output_collection": "all",
            },
            "runtime": {
                "env": list(task.env_names),
                "setup": list(task.setup_commands),
                "thinking": task.thinking,
            },
            "legacy_workspace": task.legacy_workspace,
            "resume_safe": context_manifest["resume_safe"],
        }
        manifest_digest = _canonical_digest(manifest)
        manifest["manifest_digest"] = manifest_digest
        manifest_path = task_root / "task.json"
        atomic_write_json(manifest_path, manifest)
        return TaskBundle(
            root=task_root,
            inputs_root=inputs_root,
            context_root=context_root,
            manifest_path=manifest_path,
            input_manifest_path=input_manifest_path,
            context_manifest_path=context_manifest_path,
            manifest_digest=manifest_digest,
            input_tree_digest=input_manifest["tree_digest"],
            input_manifest_digest=input_manifest_digest,
            context_manifest_digest=context_manifest_digest,
            legacy_workspace=task.legacy_workspace,
            resume_safe=context_manifest["resume_safe"],
        )

    def prepare_all(self, tasks: Iterable[Task]) -> dict[str, TaskBundle]:
        return {task.id: self.prepare(task) for task in tasks}

    def attach(
        self, requests: Iterable[RunRequest], bundles: dict[str, TaskBundle]
    ) -> list[RunRequest]:
        return [replace(request, bundle=bundles[request.task.id]) for request in requests]

    def cleanup_runtime(self) -> None:
        if self.runtime_root.exists():
            shutil.rmtree(self.runtime_root)
