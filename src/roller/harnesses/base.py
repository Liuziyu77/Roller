from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from roller.models import HarnessResult, RunRequest


class Harness(ABC):
    """Interface implemented by every trajectory-producing harness adapter."""

    name: str

    @abstractmethod
    def run(self, request: RunRequest, output_dir: Path, attempt: int) -> HarnessResult:
        """Execute one task/model request and persist its harness-native artifacts."""


@dataclass(frozen=True)
class TurnResult:
    """Visible assistant reply produced by one interactive harness turn."""

    content: str
    elapsed_seconds: float
    return_code: int = 0


class InteractiveSession(ABC):
    """A live harness session that accepts one message at a time."""

    @abstractmethod
    def start(self) -> None:
        """Allocate and configure the underlying harness runtime."""

    @abstractmethod
    def send(self, message: str, timeout_seconds: float) -> TurnResult:
        """Send one user message and return the final visible assistant reply."""

    @abstractmethod
    def finish(self, status: str, error: str | None = None) -> HarnessResult:
        """Collect traces and outputs, then release runtime resources."""


class InteractiveHarness(Harness):
    """Harness capable of exposing a live session to a conversation manager."""

    @abstractmethod
    def create_session(
        self,
        request: RunRequest,
        output_dir: Path,
        attempt: int,
        *,
        collect_outputs: bool = True,
    ) -> InteractiveSession:
        """Create an unstarted interactive session."""
