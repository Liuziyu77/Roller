from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Sequence


class DockerRuntime:
    """Small Docker subprocess boundary that is replaceable in tests."""

    def __init__(self, binary: str = "docker") -> None:
        self.binary = binary

    def run(
        self, args: Sequence[str], timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def popen(
        self, args: Sequence[str], log_path: Path, append: bool = False
    ) -> subprocess.Popen[str]:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("a" if append else "w", encoding="utf-8")
        process = subprocess.Popen(
            [self.binary, *args],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        process._roller_log_file = log_file  # type: ignore[attr-defined]
        return process

    @staticmethod
    def close_log(process: subprocess.Popen[str] | None) -> None:
        if process is None:
            return
        log_file = getattr(process, "_roller_log_file", None)
        if log_file is not None and not log_file.closed:
            log_file.close()


def warn_if_image_changed(
    logger: logging.Logger,
    image: str,
    preflight_digest: str | None,
    rollout_digest: str | None,
) -> None:
    if (
        preflight_digest
        and rollout_digest
        and preflight_digest != rollout_digest
    ):
        logger.warning(
            "Docker image %s changed after preflight: expected %s, found %s; "
            "the rollout uses and records the current image",
            image,
            preflight_digest,
            rollout_digest,
        )


def remove_container(
    runtime: DockerRuntime,
    name: str,
    *,
    timeouts: Sequence[float] = (30, 60),
) -> subprocess.CompletedProcess[str]:
    """Remove a task container, retrying transient Docker/Podman failures once."""

    if not timeouts:
        raise ValueError("container removal requires at least one timeout")
    last_result: subprocess.CompletedProcess[str] | None = None
    last_error: subprocess.TimeoutExpired | None = None
    for timeout in timeouts:
        try:
            last_result = runtime.run(["rm", "-f", name], timeout=timeout)
            last_error = None
        except subprocess.TimeoutExpired as exc:
            last_error = exc
            continue
        if last_result.returncode == 0:
            return last_result
    if last_error is not None:
        raise last_error
    assert last_result is not None
    return last_result
