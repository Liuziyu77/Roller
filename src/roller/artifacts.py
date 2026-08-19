from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from roller.models import OutputFile
from roller.storage import atomic_write_json


@dataclass(frozen=True)
class ArtifactCollectionResult:
    passed: bool
    manifest_path: Path
    validation_path: Path
    declared_files: int
    undeclared_files: int
    errors: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "manifest_path": str(self.manifest_path),
            "validation_path": str(self.validation_path),
            "declared_files": self.declared_files,
            "undeclared_files": self.undeclared_files,
            "errors": list(self.errors),
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _walk_outputs(root: Path) -> tuple[dict[str, Path], list[dict[str, Any]]]:
    files: dict[str, Path] = {}
    errors: list[dict[str, Any]] = []
    if not root.exists():
        return files, errors
    for current, dir_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        retained_dirs: list[str] = []
        for name in sorted(dir_names):
            directory = current_path / name
            relative = directory.relative_to(root).as_posix()
            if directory.is_symlink():
                errors.append({"type": "unsafe_symlink", "path": relative})
            else:
                retained_dirs.append(name)
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
    return files, errors


def _files_under(path: Path, root: Path, known_files: dict[str, Path]) -> set[str]:
    if path.is_file() and not path.is_symlink():
        return {path.relative_to(root).as_posix()}
    if path.is_dir() and not path.is_symlink():
        prefix = path.relative_to(root).as_posix()
        if prefix == ".":
            return set(known_files)
        return {
            relative
            for relative in known_files
            if relative == prefix or relative.startswith(f"{prefix}/")
        }
    return set()


def collect_output_artifacts(
    staging_root: Path,
    sample_dir: Path,
    output_specs: Iterable[OutputFile],
) -> ArtifactCollectionResult:
    """Validate and partition generated outputs into declared and undeclared artifacts."""

    artifact_root = sample_dir / "artifacts"
    declared_root = artifact_root / "declared"
    undeclared_root = artifact_root / "undeclared"
    if artifact_root.exists():
        shutil.rmtree(artifact_root)
    declared_root.mkdir(parents=True, exist_ok=True)
    undeclared_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)

    known_files, errors = _walk_outputs(staging_root)
    declared_by: dict[str, list[str]] = {}
    declared_directories: set[str] = set()
    declaration_rows: list[dict[str, Any]] = []

    for index, spec in enumerate(output_specs):
        declaration_id = f"output-{index}"
        matched_files: set[str] = set()
        present = False
        candidate = staging_root / spec.path

        if spec.type == "file":
            present = candidate.exists() or candidate.is_symlink()
            if present and (candidate.is_symlink() or not candidate.is_file()):
                errors.append(
                    {"type": "output_type_mismatch", "declaration": declaration_id, "path": spec.path}
                )
            else:
                matched_files = _files_under(candidate, staging_root, known_files)
        elif spec.type == "directory":
            present = candidate.exists() or candidate.is_symlink()
            if present and (candidate.is_symlink() or not candidate.is_dir()):
                errors.append(
                    {"type": "output_type_mismatch", "declaration": declaration_id, "path": spec.path}
                )
            else:
                matched_files = _files_under(candidate, staging_root, known_files)
                if present and spec.path != ".":
                    declared_directories.add(spec.path)
        else:
            matches = list(staging_root.glob(spec.path))
            present = bool(matches)
            for match in matches:
                matched_files.update(_files_under(match, staging_root, known_files))
                if match.is_dir() and not match.is_symlink():
                    relative_directory = match.relative_to(staging_root).as_posix()
                    if relative_directory != ".":
                        declared_directories.add(relative_directory)

        if spec.required and not present:
            errors.append(
                {"type": "required_output_missing", "declaration": declaration_id, "path": spec.path}
            )
        for relative in sorted(matched_files):
            declared_by.setdefault(relative, []).append(declaration_id)
            size = known_files[relative].stat().st_size
            if spec.max_bytes is not None and size > spec.max_bytes:
                errors.append(
                    {
                        "type": "output_too_large",
                        "declaration": declaration_id,
                        "path": relative,
                        "size": size,
                        "max_bytes": spec.max_bytes,
                    }
                )
        declaration_rows.append(
            {
                "id": declaration_id,
                "path": spec.path,
                "type": spec.type,
                "required": spec.required,
                "max_bytes": spec.max_bytes,
                "present": present,
                "matched_files": sorted(matched_files),
            }
        )

    for directory in sorted(declared_directories):
        (declared_root / directory).mkdir(parents=True, exist_ok=True)

    file_rows: list[dict[str, Any]] = []
    for relative, source in sorted(known_files.items()):
        declared = relative in declared_by
        destination_root = declared_root if declared else undeclared_root
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        mime_type, _ = mimetypes.guess_type(relative)
        file_rows.append(
            {
                "path": relative,
                "classification": "declared" if declared else "undeclared",
                "declared_by": declared_by.get(relative, []),
                "sha256": _sha256(source),
                "size": source.stat().st_size,
                "mime_type": mime_type or "application/octet-stream",
            }
        )

    manifest_path = sample_dir / "outputs.manifest.json"
    validation_path = sample_dir / "artifact_validation.json"
    manifest = {
        "schema_version": "roller.outputs/v1",
        "declarations": declaration_rows,
        "declared_directories": sorted(declared_directories),
        "files": file_rows,
    }
    validation = {
        "passed": not errors,
        "declared_files": sum(row["classification"] == "declared" for row in file_rows),
        "undeclared_files": sum(row["classification"] == "undeclared" for row in file_rows),
        "errors": errors,
    }
    atomic_write_json(manifest_path, manifest)
    atomic_write_json(validation_path, validation)
    return ArtifactCollectionResult(
        passed=validation["passed"],
        manifest_path=manifest_path,
        validation_path=validation_path,
        declared_files=validation["declared_files"],
        undeclared_files=validation["undeclared_files"],
        errors=tuple(errors),
    )
