"""Shared lifecycle for call capture in disposable harness sessions."""
from pathlib import Path
import shutil

from roller.capture import CallRecorder


class CaptureRuntime:
    def __init__(self, output_dir: Path, attempt: int, harness: str, debug_retention: str = "on-error"):
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

    def finish(self, status: str) -> dict:
        self.recorder.seal()
        try:
            if self.harness == "codex":
                from roller.codex_capture import read_codex_native
                native = read_codex_native(self.recorder.directory)
            elif self.harness == "openclaw":
                from roller.openclaw_capture import read_openclaw_native
                native = read_openclaw_native(self.recorder.directory)
            else:
                from roller.bridge_capture import read_bridge_native
                native = read_bridge_native(self.recorder.directory, self.harness)
            from roller.capture_protocol import finalize_capture
            native.errors.extend(self.errors)
            manifest = finalize_capture(self.recorder.directory, native, execution_status=status,
                                        recorder_error=self.recorder.error, debug_retention=self.debug_retention)
            return {"capture": {"manifest": (self.recorder.directory / "manifest.json").relative_to(self.output_dir).as_posix(),
                                "capture_status": manifest["capture_status"]}}
        except Exception as exc:
            return {"capture": {"manifest": None, "capture_status": "partial", "error": type(exc).__name__}}
