"""Shared lifecycle for call capture in disposable harness sessions."""
from pathlib import Path
from typing import Callable
import shutil

from roller.capture import CallRecorder
from roller.capture_states import CaptureStatus
from roller.harness_registry import HARNESSES, read_native_capture


class CaptureRuntime:
    def __init__(self, output_dir: Path, attempt: int, harness: str, debug_retention: str = "on-error"):
        if harness not in HARNESSES:
            raise ValueError(f"unknown capture harness: {harness}")
        self.output_dir = output_dir
        self.harness = harness
        self.debug_retention = debug_retention
        directory = output_dir / "capture" / f"attempt-{attempt:04d}"
        if directory.exists():
            shutil.rmtree(directory)
        self.recorder = CallRecorder(directory)
        self.errors: list[str] = []

    def copy_native(self, runtime, container: str, source: str, name: str) -> None:
        destination = self.recorder.directory / "native"
        destination.mkdir(parents=True, exist_ok=True)
        result = runtime.run(["cp", f"{container}:{source}", str(destination / name)], timeout=30)
        if result.returncode:
            self.errors.append(f"native evidence collection failed: {name}")

    def add_native(self, source: Path, name: str = "trace.jsonl") -> None:
        destination = self.recorder.directory / "native"
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination / name)

    def finish(self, status: str, *, error_formatter: Callable[[Exception], str] | None = None) -> dict:
        self.recorder.seal()
        try:
            from roller.capture_protocol import finalize_native_capture
            manifest = finalize_native_capture(self.recorder.directory,
                lambda directory: read_native_capture(self.harness, directory),
                execution_status=status, recorder_error=self.recorder.error,
                debug_retention=self.debug_retention, native_errors=self.errors)
            return {"capture": {"manifest": (self.recorder.directory / "manifest.json").relative_to(self.output_dir).as_posix(),
                                "capture_status": manifest["capture_status"]}}
        except Exception as exc:
            error = error_formatter(exc) if error_formatter else type(exc).__name__
            return {"capture": {"manifest": None, "capture_status": CaptureStatus.PARTIAL, "error": error}}
