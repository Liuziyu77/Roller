from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

IGNORED_OUTPUT_DIRECTORIES = frozenset({"node_modules"})


@dataclass(frozen=True)
class ArtifactCollectionResult:
    passed: bool
    results_path: Path | None
    file_count: int
    directory_count: int
    files: tuple[dict[str, Any], ...]
    errors: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "path": str(self.results_path) if self.results_path is not None else None,
            "file_count": self.file_count,
            "directory_count": self.directory_count,
            "files": list(self.files),
            "errors": list(self.errors),
        }


def _copy_with_sha256(source: Path, destination: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as input_stream, destination.open("wb") as output_stream:
        while chunk := input_stream.read(1024 * 1024):
            output_stream.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    shutil.copystat(source, destination, follow_symlinks=False)
    return digest.hexdigest(), size


def _walk_outputs(
    root: Path,
) -> tuple[dict[str, Path], set[str], list[dict[str, Any]]]:
    files: dict[str, Path] = {}
    directories: set[str] = set()
    errors: list[dict[str, Any]] = []
    if not root.exists():
        return files, directories, errors
    for current, dir_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        retained_dirs: list[str] = []
        for name in sorted(dir_names):
            if name in IGNORED_OUTPUT_DIRECTORIES:
                continue
            directory = current_path / name
            relative = directory.relative_to(root).as_posix()
            if directory.is_symlink():
                errors.append({"type": "unsafe_symlink", "path": relative})
            else:
                retained_dirs.append(name)
                directories.add(relative)
        dir_names[:] = retained_dirs
        for name in sorted(file_names):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                errors.append({"type": "unsafe_symlink", "path": relative})
            elif not path.is_file():
                errors.append({"type": "non_regular_file", "path": relative})
            else:
                files[relative] = path
    return files, directories, errors


def collect_output_directory(
    staging_root: Path,
    sample_dir: Path,
) -> ArtifactCollectionResult:
    """Safely copy every regular file from the task output directory to ``results/``."""

    results_root = sample_dir / "results"
    if results_root.exists():
        shutil.rmtree(results_root)
    staging_root.mkdir(parents=True, exist_ok=True)

    known_files, directories, errors = _walk_outputs(staging_root)
    if not known_files and not directories:
        return ArtifactCollectionResult(
            passed=not errors,
            results_path=None,
            file_count=0,
            directory_count=0,
            files=(),
            errors=tuple(errors),
        )

    results_root.mkdir(parents=True, exist_ok=True)
    for directory in sorted(directories):
        (results_root / directory).mkdir(parents=True, exist_ok=True)

    file_rows: list[dict[str, Any]] = []
    for relative, source in sorted(known_files.items()):
        destination = results_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        sha256, size = _copy_with_sha256(source, destination)
        mime_type, _ = mimetypes.guess_type(relative)
        file_rows.append(
            {
                "path": relative,
                "sha256": sha256,
                "size": size,
                "mime_type": mime_type or "application/octet-stream",
            }
        )

    return ArtifactCollectionResult(
        passed=not errors,
        results_path=results_root,
        file_count=len(file_rows),
        directory_count=len(directories),
        files=tuple(file_rows),
        errors=tuple(errors),
    )
