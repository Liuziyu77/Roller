from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from roller.models import HarnessResult, RunRequest


class Harness(ABC):
    """Interface implemented by every trajectory-producing harness adapter."""

    name: str

    @abstractmethod
    def run(self, request: RunRequest, output_dir: Path, attempt: int) -> HarnessResult:
        """Execute one task/model request and persist its harness-native artifacts."""

