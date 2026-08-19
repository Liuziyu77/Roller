from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class InputFile:
    """One immutable task input, resolved on the host before execution."""

    source: Path
    target: str
    required: bool = True
    expected_sha256: str | None = None


@dataclass(frozen=True)
class ContextFile:
    """A semantic system/context file injected by a harness adapter."""

    role: str
    source: Path
    target: str
    capture: str = "full"
    required: bool = True


@dataclass(frozen=True)
class OutputFile:
    """A declared output contract relative to the Roller outputs directory."""

    path: str
    type: str = "file"
    required: bool = True
    max_bytes: int | None = None


@dataclass(frozen=True)
class TaskBundle:
    """Materialized, content-addressed files shared by all models for one task."""

    root: Path
    inputs_root: Path
    context_root: Path
    manifest_path: Path
    input_manifest_path: Path
    context_manifest_path: Path
    manifest_digest: str
    input_tree_digest: str
    input_manifest_digest: str
    context_manifest_digest: str
    legacy_workspace: bool = False
    resume_safe: bool = True


@dataclass(frozen=True)
class Task:
    """A normalized task loaded from a JSON or JSONL task list."""

    id: str
    prompts: tuple[str, ...]
    source_index: int
    raw: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = "roller.task/v1"
    inputs: tuple[InputFile, ...] = ()
    context_files: tuple[ContextFile, ...] = ()
    outputs: tuple[OutputFile, ...] = ()
    legacy_workspace: bool = False
    workspace: Path | None = None
    model: str | None = None
    env_names: tuple[str, ...] = ()
    setup_commands: tuple[str, ...] = ()
    thinking: str | None = None


@dataclass(frozen=True)
class RunRequest:
    """One task/model pair to be executed by a harness."""

    index: int
    task: Task
    model: str
    timeout_seconds: int
    bundle: TaskBundle | None = None


@dataclass
class HarnessResult:
    """Harness-independent result returned for one trajectory."""

    status: str
    elapsed_seconds: float
    error: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    trace_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def error_result(cls, error: str, elapsed_seconds: float = 0.0) -> "HarnessResult":
        return cls(status="error", elapsed_seconds=elapsed_seconds, error=error)
