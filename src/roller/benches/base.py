from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

BENCH_SCHEMA = "roller.bench/v1"
DEFAULT_GRADE_TIMEOUT = 120
MAX_GRADE_TIMEOUT = 3600


class BenchFormatError(ValueError):
    """Raised when a bench task list or grade block cannot be understood."""


@dataclass(frozen=True)
class GradeSpec:
    """A scorer that runs inside the sample's own container before teardown.

    Benches ship their grader as Python source rather than as a host-side
    callable: the score usually depends on the container filesystem the agent
    just wrote to, which no longer exists once the harness tears it down.
    """

    code: str
    entrypoint: str = "grade"
    kwargs: dict[str, Any] = field(default_factory=dict)
    env_names: tuple[str, ...] = ()
    timeout_seconds: int = DEFAULT_GRADE_TIMEOUT
    needs_transcript: bool = True
    workspace_path: str | None = None
    transcript_path: str | None = None
    score_key: str = "overall_score"
    # Host paths copied into the container immediately before grading, keyed by
    # their destination relative to the grading workspace. Answer keys live here
    # rather than in the task's workspace: anything the agent can read it can
    # also copy from, and a bench whose expected output ships alongside its seed
    # files is not measuring the model.
    stage: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code}
        if self.entrypoint != "grade":
            payload["entrypoint"] = self.entrypoint
        if self.kwargs:
            payload["kwargs"] = dict(self.kwargs)
        if self.env_names:
            payload["env"] = list(self.env_names)
        if self.timeout_seconds != DEFAULT_GRADE_TIMEOUT:
            payload["timeout_seconds"] = self.timeout_seconds
        if not self.needs_transcript:
            payload["needs_transcript"] = False
        if self.workspace_path is not None:
            payload["workspace_path"] = self.workspace_path
        if self.transcript_path is not None:
            payload["transcript_path"] = self.transcript_path
        if self.score_key != "overall_score":
            payload["score_key"] = self.score_key
        if self.stage:
            payload["stage"] = [
                {"source": source, "target": target} for source, target in self.stage
            ]
        return payload

    @classmethod
    def from_dict(cls, payload: Any, label: str = "bench.grade") -> "GradeSpec":
        if not isinstance(payload, dict):
            raise BenchFormatError(f"{label}: must be an object")
        code = payload.get("code")
        if not isinstance(code, str) or not code.strip():
            raise BenchFormatError(f"{label}.code: must be non-empty Python source")
        entrypoint = payload.get("entrypoint", "grade")
        if not isinstance(entrypoint, str) or not entrypoint.isidentifier():
            raise BenchFormatError(f"{label}.entrypoint: must be a Python identifier")
        kwargs = payload.get("kwargs", {})
        if not isinstance(kwargs, dict):
            raise BenchFormatError(f"{label}.kwargs: must be an object")
        env_names = _env_names(payload.get("env"), f"{label}.env")
        timeout_seconds = payload.get("timeout_seconds", DEFAULT_GRADE_TIMEOUT)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= MAX_GRADE_TIMEOUT
        ):
            raise BenchFormatError(
                f"{label}.timeout_seconds: must be an integer from 1 to {MAX_GRADE_TIMEOUT}"
            )
        needs_transcript = payload.get("needs_transcript", True)
        if not isinstance(needs_transcript, bool):
            raise BenchFormatError(f"{label}.needs_transcript: must be boolean")
        workspace_path = payload.get("workspace_path")
        if workspace_path is not None and (
            not isinstance(workspace_path, str) or not workspace_path.startswith("/")
        ):
            raise BenchFormatError(
                f"{label}.workspace_path: must be an absolute container path"
            )
        transcript_path = payload.get("transcript_path")
        if transcript_path is not None and (
            not isinstance(transcript_path, str) or not transcript_path.startswith("/")
        ):
            raise BenchFormatError(
                f"{label}.transcript_path: must be an absolute container path"
            )
        score_key = payload.get("score_key", "overall_score")
        if not isinstance(score_key, str) or not score_key.strip():
            raise BenchFormatError(f"{label}.score_key: must be a non-empty string")
        return cls(
            code=code,
            entrypoint=entrypoint,
            kwargs=kwargs,
            env_names=env_names,
            timeout_seconds=timeout_seconds,
            needs_transcript=needs_transcript,
            workspace_path=workspace_path,
            transcript_path=transcript_path,
            score_key=score_key.strip(),
            stage=_stage_files(payload.get("stage"), f"{label}.stage"),
        )


def _stage_files(value: Any, label: str) -> tuple[tuple[str, str], ...]:
    """Parse ``stage`` entries into ``(host source, workspace-relative target)``."""

    if value is None:
        return ()
    if not isinstance(value, list):
        raise BenchFormatError(f"{label}: must be a list")
    entries: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        entry_label = f"{label}[{index}]"
        if not isinstance(item, dict):
            raise BenchFormatError(f"{entry_label}: must be an object")
        source = item.get("source")
        if not isinstance(source, str) or not source.startswith("/"):
            raise BenchFormatError(f"{entry_label}.source: must be an absolute host path")
        target = item.get("target")
        if not isinstance(target, str) or not target.strip():
            raise BenchFormatError(f"{entry_label}.target: must be a non-empty string")
        target = target.strip()
        parts = PurePosixPath(target).parts
        # A target that escapes the workspace would let a bench definition write
        # anywhere in the container, so reject rather than normalize it away.
        if target.startswith("/") or not parts or any(part == ".." for part in parts):
            raise BenchFormatError(
                f"{entry_label}.target: must be a relative path inside the workspace"
            )
        entries.append((source, target))
    return tuple(entries)


def _env_names(value: Any, label: str) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        names = [line.strip() for line in value.splitlines()]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        names = [item.strip() for item in value]
    else:
        raise BenchFormatError(f"{label}: must be a string or string list")
    return tuple(name for name in names if name and not name.startswith("#"))


@dataclass(frozen=True)
class BenchBinding:
    """The ``bench`` block carried by one exported task row."""

    name: str
    grade: GradeSpec | None = None
    category: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"schema_version": BENCH_SCHEMA, "name": self.name}
        if self.category:
            payload["category"] = self.category
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        if self.grade is not None:
            payload["grade"] = self.grade.as_dict()
        return payload

    @classmethod
    def from_dict(cls, payload: Any, label: str = "bench") -> "BenchBinding":
        if not isinstance(payload, dict):
            raise BenchFormatError(f"{label}: must be an object")
        schema_version = payload.get("schema_version", BENCH_SCHEMA)
        if schema_version != BENCH_SCHEMA:
            raise BenchFormatError(
                f"{label}.schema_version: unsupported {schema_version!r}; "
                f"expected {BENCH_SCHEMA!r}"
            )
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise BenchFormatError(f"{label}.name: must be a non-empty string")
        category = payload.get("category")
        if category is not None and not isinstance(category, str):
            raise BenchFormatError(f"{label}.category: must be a string")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            raise BenchFormatError(f"{label}.metadata: must be an object")
        grade_payload = payload.get("grade")
        grade = (
            GradeSpec.from_dict(grade_payload, f"{label}.grade")
            if grade_payload is not None
            else None
        )
        return cls(
            name=name.strip(),
            grade=grade,
            category=category,
            metadata=metadata,
        )


def binding_from_task_row(row: dict[str, Any]) -> BenchBinding | None:
    """Read the optional ``bench`` block from a ``roller.task/v1`` row."""

    payload = row.get("bench") if isinstance(row, dict) else None
    if payload is None:
        return None
    return BenchBinding.from_dict(payload)


class BenchAdapter(ABC):
    """Turn an upstream benchmark into ``roller.task/v1`` rows plus scorers.

    ``load()`` translates a checked-out benchmark into task rows that Roller's
    collect path understands. ``wrap_harness()`` may adapt execution for
    benches that require per-task runtime images or workspaces.
    """

    name: str
    description: str = ""

    @abstractmethod
    def load(
        self,
        root: Path,
        *,
        categories: Sequence[str] | None = None,
        task_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return ``roller.task/v1`` rows, each with a ``bench`` block."""

    def categories(self, root: Path) -> list[str]:
        """List the selectable category names, if the bench has any."""

        seen: dict[str, None] = {}
        for row in self.load(root):
            binding = binding_from_task_row(row)
            if binding is not None and binding.category:
                seen.setdefault(binding.category, None)
        return list(seen)

    def wrap_harness(self, harness: Any, binding: BenchBinding) -> Any:
        """Prepare a harness for this bench and attach its live-container grader."""

        if binding.grade is None:
            return harness
        from roller.benches.grading import GradedHarness

        return GradedHarness(harness, binding)

    def aggregate(self, samples: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Summarize per-sample scores; override to match upstream reporting."""

        from roller.benches.report import mean_aggregate

        return mean_aggregate(samples)
