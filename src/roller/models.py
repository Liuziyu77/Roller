from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from roller.thinking import normalize_thinking


@dataclass(frozen=True)
class InputFile:
    """One immutable task input, resolved on the host before execution."""

    source: Path | None
    path: str
    required: bool = True
    expected_sha256: str | None = None
    # Small benchmark fixtures may be embedded in the task row.  Keeping them
    # here avoids mutating an upstream checkout just to materialize YAML block
    # scalars before the normal content-addressed bundle step.
    content: str | None = None


@dataclass(frozen=True)
class ContainerImageBuild:
    """A reproducible task environment that Roller can build on demand.

    ``base_image`` is built from the upstream benchmark context. ``image`` is
    the final image after the selected Roller harness layer has been added.
    Currently the packaged overlay is intentionally explicit about the
    harness it supports, so a CLI override cannot silently run an incompatible
    binary in the task environment.
    """

    image: str
    base_image: str
    context: Path
    dockerfile: str
    harness: str
    build_args: tuple[tuple[str, str], ...] = ()
    timeout_seconds: int = 3600


@dataclass(frozen=True)
class ContextFile:
    """A semantic system/context file injected by a harness adapter."""

    role: str
    source: Path
    target: str
    capture: str = "full"
    required: bool = True


@dataclass(frozen=True)
class TaskBundle:
    """Materialized, content-addressed files shared by all models for one task."""

    root: Path
    inputs_root: Path
    context_root: Path
    manifest_digest: str
    input_tree_digest: str
    input_manifest_digest: str
    context_manifest_digest: str
    legacy_workspace: bool = False
    resume_safe: bool = True


@dataclass(frozen=True)
class UserSimulatorSpec:
    """Configuration for a model-driven user in an interactive rollout."""

    instruction: str
    harness: str = "openclaw"
    model: str | None = None
    max_turns: int = 6
    thinking: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "thinking",
            normalize_thinking(self.thinking, field="user simulator thinking"),
        )


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
    output_dir: str = "outputs"
    legacy_workspace: bool = False
    workspace: Path | None = None
    model: str | None = None
    env_names: tuple[str, ...] = ()
    setup_commands: tuple[str, ...] = ()
    # ``True`` means the corresponding prompt starts with fresh conversation
    # state while retaining the same live container and workspace.
    new_session: tuple[bool, ...] = ()
    docker_image: str | None = None
    image_build: ContainerImageBuild | None = None
    workspace_container_path: str | None = None
    thinking: str | None = None
    user_simulator: UserSimulatorSpec | None = None
    # Harness used by the task agent. Kept after all historical fields for
    # positional constructor compatibility with earlier Roller releases.
    harness: str = "openclaw"
    # Explicit inputs are immutable bind mounts by default. Benchmarks that
    # ask the agent to edit seed files can request a writable workspace copy.
    input_mode: str = "mount"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "thinking",
            normalize_thinking(self.thinking, field="task thinking"),
        )


@dataclass(frozen=True)
class RunRequest:
    """One task/model pair to be executed by a harness."""

    index: int
    task: Task
    model: str
    timeout_seconds: int
    bundle: TaskBundle | None = None
    # Zero-based independent repetition of the same task/model pair.  Keeping
    # this on the request (rather than cloning the Task with a fake id) lets
    # benchmark reports aggregate repeated trials under the canonical task id.
    sample_index: int = 0
    sample_count: int = 1
    # Optional safe/local label for paths and container names. The harness
    # still receives ``model`` unchanged, so exact external served IDs remain
    # auditable and are sent to the provider verbatim.
    model_label: str | None = None

    def __post_init__(self) -> None:
        if self.model_label is not None and not self.model_label.strip():
            raise ValueError("model_label must be non-empty when provided")

    @property
    def display_model(self) -> str:
        return self.model_label.strip() if self.model_label is not None else self.model


@dataclass
class HarnessResult:
    """Harness-independent result returned for one trajectory."""

    status: str
    elapsed_seconds: float
    error: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    trace_path: str | None = None
    trajectory_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def error_result(cls, error: str, elapsed_seconds: float = 0.0) -> "HarnessResult":
        return cls(status="error", elapsed_seconds=elapsed_seconds, error=error)
