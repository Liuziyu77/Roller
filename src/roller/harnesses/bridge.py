"""Shared lifecycle for Harnesses driven by a pinned in-container bridge runner.

Hermes, OpenCode, DeepSeek and Pi each ship a ``runner.py`` under
``roller/assets`` that executes one turn from ``turn.json``, writes
``result.json`` and appends to a native journal. This module owns the Docker
side of that contract; subclasses declare what differs per Harness.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shlex
import subprocess
import time
from typing import Any, ClassVar, Sequence
from urllib.parse import urlsplit
import uuid

from roller.capture_runtime import CaptureRuntime
from roller.harnesses.base import InteractiveHarness, InteractiveSession, TurnResult
from roller.harnesses.container import (
    CONTEXT_CONTAINER_PATH,
    ENV_NAME,
    INPUTS_CONTAINER_PATH,
    OUTPUTS_CONTAINER_PATH,
    WORKSPACE_INPUT_CONTAINER_PATH,
    build_provenance,
    collect_outputs,
    docker_run_args,
    output_collection_metadata,
)
from roller.harnesses.docker import DockerRuntime, remove_container
from roller.models import HarnessResult, RunRequest
from roller.storage import atomic_write_json
from roller.thinking import resolve_thinking
from roller.usage import ZERO_USAGE
from roller.wire_trace import WireTraceProxy, redact_jsonl_file

logger = logging.getLogger(__name__)


def validate_bridge_config(config: Any, label: str) -> None:
    """Checks shared by every bridge Harness config; limits stay per Harness."""

    if not config.docker_image.strip():
        raise ValueError(f"{label} docker image must be non-empty")
    if config.container_user is not None and not config.container_user.strip():
        raise ValueError(f"{label} container user must be non-empty or None")
    if not config.workspace_container_path.startswith("/"):
        raise ValueError(f"{label} workspace container path must be absolute")
    if not all(ENV_NAME.fullmatch(name) for name in (config.api_key_env, *config.pass_env)):
        raise ValueError(f"invalid {label} environment variable name")
    if config.api_base:
        try:
            parsed = urlsplit(config.api_base)
            parsed.port
            valid = parsed.scheme in {"http", "https"} and parsed.hostname and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f"{label} API base must be an absolute http(s) URL without credentials, query or fragment")
    if config.wire_trace_mode not in {"off", "actf", "full"}:
        raise ValueError(f"{label} wire trace mode must be off|actf|full")
    if config.wire_trace_mode == "full" and (not config.api_base or config.network != "host"):
        raise ValueError(f"{label} --wire-trace full requires --api-base and --network host")
    if config.capture_calls and (not config.api_base or config.network != "host" or config.wire_trace_mode == "off"):
        raise ValueError(f"{label} call capture requires --api-base, --network host and wire tracing")


def positive_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 1


def validate_step_limits(config: Any, label: str) -> None:
    """``max_steps``/``context_length``/``output_tokens`` used by the step-based runners."""

    if not positive_int(config.max_steps):
        raise ValueError(f"{label} max steps must be a positive integer")
    if not positive_int(config.context_length) or config.context_length < 1024:
        raise ValueError(f"{label} context length must be at least 1024 tokens")
    if not positive_int(config.output_tokens) or config.output_tokens >= config.context_length:
        raise ValueError(f"{label} output tokens must be positive and smaller than context length")


class BridgeSession(InteractiveSession):
    """One disposable container in which the bridge runner keeps native state."""

    label: ClassVar[str]
    state_dir: ClassVar[str]
    runner: ClassVar[str]
    runner_source: ClassVar[Path]
    raw_trace_schema: ClassVar[str]
    python: ClassVar[str] = "python3"
    api_key_name: ClassVar[str] = "OPENAI_API_KEY"
    default_api_base: ClassVar[str] = "https://api.openai.com/v1"
    version_check: ClassVar[str] = "version check"
    # (file next to runner_source, container destination, description)
    assets: ClassVar[tuple[tuple[str, str, str], ...]] = ()
    native_trace_name: ClassVar[str] = "trace.jsonl"
    redact_trace: ClassVar[bool] = False
    conversion_failure_is_error: ClassVar[bool] = True
    redact_capture_errors: ClassVar[bool] = False
    supports_reset: ClassVar[bool] = False

    def __init__(
        self, harness: BridgeHarness, request: RunRequest, output_dir: Path,
        attempt: int, *, collect_outputs: bool = True,
    ) -> None:
        self.harness, self.request = harness, request
        self.output_dir, self.attempt = Path(output_dir), attempt
        self.collect_outputs = collect_outputs
        self.container = f"roller-{harness.name}-{request.index}-{uuid.uuid4().hex[:12]}"
        self.container_started = False
        self.transcript_container_path = self.state_dir + "/trace.jsonl"
        self.home = f"/roller/{harness.name}-home"
        self.started = time.monotonic()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.environment: dict[str, str] = {}
        self.image_digest: str | None = None
        self.version: str | None = None
        self.proxy: WireTraceProxy | None = None
        config = harness.config
        self.capture = CaptureRuntime(self.output_dir, attempt, harness.name, config.capture_debug) if config.capture_calls else None
        self.capture_metadata: dict[str, Any] = {}
        self.warnings: list[str] = []
        self.trajectory_conversion: dict[str, Any] | None = None
        self._result: HarnessResult | None = None
        self.turns = 0

    # Per-Harness declarations -------------------------------------------------

    def effort(self) -> str | None:
        """The validated native effort for this task."""

        raise NotImplementedError

    def harness_environment(self, api_base: str) -> dict[str, str]:
        raise NotImplementedError

    def turn_options(self, timeout: float) -> dict[str, Any]:
        """Harness-specific ``turn.json`` fields."""

        raise NotImplementedError

    def convert_trace(self, trace: Path, destination: Path) -> dict[str, Any]:
        raise NotImplementedError

    def extract_usage(self, trace: Path) -> dict[str, int | float]:
        raise NotImplementedError

    def version_fields(self) -> dict[str, Any]:
        raise NotImplementedError

    def settings(self) -> dict[str, Any]:
        raise NotImplementedError

    def setup_script(self) -> str:
        """Extra shell steps run after the state directories exist."""

        return ""

    def inherited_environment(self) -> dict[str, str]:
        config = self.harness.config
        return {name: os.environ[name] for name in config.pass_env if os.environ.get(name)}

    def system_prompt(self) -> str:
        config, task = self.harness.config, self.request.task
        context = ", ".join(item.target for item in task.context_files)
        return (
            f"You are running a Roller task. Work in {config.workspace_container_path}. "
            f"Task inputs are in the workspace. Save deliverables under {task.output_dir}. "
            f"Task context files (read when relevant): {context or '(none)'}. "
            "Complete the requested task and return a concise final answer."
        )

    def collect_diagnostics(self) -> None:
        """Copy extra runner logs before the journal; failures are non-fatal."""

    def collect_native_evidence(self, usage: dict[str, int | float]) -> None:
        """Add Harness-specific evidence to ``self.capture`` before it is sealed."""

    def result_metadata(self) -> dict[str, Any]:
        return {}

    # Shared lifecycle -----------------------------------------------------------

    def remaining(self) -> float:
        remaining = self.request.timeout_seconds - (time.monotonic() - self.started)
        if remaining <= 0:
            raise TimeoutError(f"{self.label} task timed out")
        return remaining

    def redact(self, text: str) -> str:
        key = os.environ.get(self.harness.config.api_key_env)
        return text.replace(key, "[REDACTED]") if key else text

    def checked(
        self, args: Sequence[str], description: str, *, timeout: float = 60,
    ) -> subprocess.CompletedProcess[str]:
        result = self.harness.runtime.run(args, timeout=min(timeout, self.remaining()))
        if result.returncode:
            detail = self.redact((result.stderr or result.stdout or "unknown error").strip())[-2000:]
            raise RuntimeError(f"{description} failed: {detail}")
        return result

    def start(self) -> None:
        if self.container_started or self._result is not None:
            raise RuntimeError(f"{self.label} session has already started or finished")
        config, task, bundle = self.harness.config, self.request.task, self.request.bundle
        self.effort()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        environment = self.inherited_environment()
        for name in task.env_names:
            if not ENV_NAME.fullmatch(name) or not os.environ.get(name):
                raise ValueError(f"task requires missing or invalid environment variable: {name}")
            environment[name] = os.environ[name]
        if not os.environ.get(config.api_key_env):
            raise ValueError(f"{self.label} requires a non-empty API key in {config.api_key_env}")
        environment[self.api_key_name] = os.environ[config.api_key_env]
        api_base = config.api_base or self.default_api_base
        if config.wire_trace_mode == "full" or config.capture_calls:
            self.proxy = WireTraceProxy(api_base, self.output_dir / "wire_trace.jsonl", mode="full",
                capture_recorder=self.capture.recorder if self.capture else None,
                capture_identity_header="x-roller-native-session-id" if self.capture else None)
            self.proxy.start()
            api_base = self.proxy.proxy_base_url
        environment.update(self.harness_environment(api_base))
        self.environment = environment
        self.image_digest = self.checked(["image", "inspect", config.docker_image, "--format", "{{.Id}}"], f"{self.label} image inspection").stdout.strip()
        args = docker_run_args(self.harness.name, self.container, config, environment, self.request)
        args += ["--entrypoint", "/bin/bash", config.docker_image, "-c", "tail -f /dev/null"]
        # Mark before run: a timed-out Docker client may already have created it.
        self.container_started = True
        self.checked(args, f"{self.label} container startup")
        workspace = shlex.quote(config.workspace_container_path)
        output = shlex.quote(f"{config.workspace_container_path}/{task.output_dir}")
        script = f"mkdir -p {workspace} {self.state_dir} {self.home} /roller/task" + self.setup_script()
        if bundle and (bundle.legacy_workspace or task.input_mode == "copy"):
            script += f" && cp -a {INPUTS_CONTAINER_PATH}/. {workspace}/ && chmod -R u+w {workspace}"
        elif task.workspace and not bundle:
            script += f" && cp -a {WORKSPACE_INPUT_CONTAINER_PATH}/. {workspace}/ && chmod -R u+w {workspace}"
        if bundle:
            script += f" && cp -a {CONTEXT_CONTAINER_PATH}/. {workspace}/"
        script += f" && rm -rf {output} && mkdir -p {output} && ln -s {output} {OUTPUTS_CONTAINER_PATH}"
        self.checked(["exec", self.container, "/bin/bash", "-c", script], f"{self.label} workspace setup")
        self.checked(["cp", str(self.runner_source), f"{self.container}:{self.runner}"], f"{self.label} runner copy")
        for name, destination, description in self.assets:
            self.checked(["cp", str(self.runner_source.with_name(name)), f"{self.container}:{destination}"], f"{self.label} {description}")
        for command in task.setup_commands:
            self.checked(["exec", "-w", config.workspace_container_path, self.container, "/bin/bash", "-c", command], "task setup", timeout=self.remaining())
        self.version = self.checked(["exec", self.container, self.python, self.runner, "--check"], f"{self.label} {self.version_check}").stdout.strip()

    def send(self, message: str, timeout_seconds: float) -> TurnResult:
        if not self.container_started or self._result is not None:
            raise RuntimeError(f"{self.label} session is not active")
        started = time.monotonic()
        timeout = min(timeout_seconds, self.remaining())
        if timeout <= 0:
            raise TimeoutError(f"{self.label} turn timed out")
        config = self.harness.config
        self.turns += 1
        payload = {
            "capture_calls": config.capture_calls, "session_id": self.container,
            "model": self.request.model, "message": message, "thinking": self.effort(),
            "timeout_seconds": timeout, "system_prompt": self.system_prompt(),
            **self.turn_options(timeout),
        }
        config_path = self.output_dir / f".{self.harness.name}-turn.json"
        atomic_write_json(config_path, payload)
        log_path = self.output_dir / "rollout" / f"{self.harness.name}.attempt-{self.attempt}.log"
        process = None
        try:
            self.checked(["cp", str(config_path), f"{self.container}:{self.state_dir}/turn.json"], f"{self.label} turn configuration")
            args = ["exec", "-w", config.workspace_container_path, self.container, self.python, self.runner, "--config", self.state_dir + "/turn.json"]
            process = self.harness.runtime.popen(args, log_path, append=True)
            try:
                return_code = process.wait(timeout=max(0.001, timeout - (time.monotonic() - started)))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                # Stop the container's exec process before collecting partial artifacts.
                self.harness.runtime.run(["kill", self.container], timeout=30)
                raise TimeoutError(f"{self.label} turn timed out") from None
            response = self.checked(["exec", self.container, "cat", self.state_dir + "/result.json"], f"{self.label} turn result")
            result = json.loads(response.stdout)
            if result.get("timeout") is True:
                self.harness.runtime.run(["kill", self.container], timeout=30)
                raise TimeoutError(f"{self.label} turn timed out")
            if return_code or result.get("ok") is not True or not isinstance(result.get("content"), str) or not result["content"].strip():
                raise RuntimeError(self.redact(str(result.get("error") or f"{self.label} runner exited with code {return_code}")))
            return TurnResult(result["content"], time.monotonic() - started, return_code)
        finally:
            config_path.unlink(missing_ok=True)
            self.harness.runtime.close_log(process)
            if log_path.exists():
                log_path.write_text(self.redact(log_path.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")

    def _collect_trace(self, problems: list[str]) -> tuple[Path | None, Path | None, dict[str, int | float]]:
        runtime = self.harness.runtime
        usage: dict[str, int | float] = dict(ZERO_USAGE)
        trace = self.output_dir / "rollout" / "trace.jsonl"
        trace.parent.mkdir(parents=True, exist_ok=True)
        copied = runtime.run(["cp", f"{self.container}:{self.transcript_container_path}", str(trace)], timeout=30)
        if copied.returncode or not trace.is_file() or not trace.stat().st_size:
            problems.append(f"no {self.label} journal was collected")
            return None, None, usage
        try:
            if self.redact_trace:
                trace.write_text(self.redact(trace.read_text(encoding="utf-8")), encoding="utf-8")
            usage = self.extract_usage(trace)
            trajectory = trace.with_name("trace_ACTF.json")
            if not self.convert_trace(trace, trajectory).get("steps"):
                raise ValueError(f"{self.label} journal produced no assistant steps")
            return trace, trajectory, usage
        except Exception as exc:
            message = f"{self.label} trace conversion failed: " + self.redact(str(exc))
            if self.conversion_failure_is_error:
                problems.append(message)
            else:
                # The native journal is retained and bench grading has already
                # observed the workspace; a missing text-only trajectory must
                # not turn a valid evaluation sample into an agent error.
                self.warnings.append(message)
                self.trajectory_conversion = {"status": "unavailable", "target": "actf", "error": self.redact(str(exc))}
            return trace, None, usage

    def finish(self, status: str, error: str | None = None) -> HarnessResult:
        if self._result is not None:
            return self._result
        config, runtime = self.harness.config, self.harness.runtime
        trace = trajectory = None
        output_metadata = None
        usage: dict[str, int | float] = dict(ZERO_USAGE)
        problems: list[str] = []
        try:
            if self.container_started:
                self.collect_diagnostics()
                try:
                    trace, trajectory, usage = self._collect_trace(problems)
                except Exception as exc:
                    problems.append(f"{self.label} trace conversion failed: " + self.redact(str(exc)))
                if self.collect_outputs:
                    collected, copy_error = collect_outputs(
                        runtime, self.container, self.request, self.output_dir,
                        config.workspace_container_path, timeout=30,
                    )
                    if copy_error is not None:
                        problems.append("output collection failed")
                    output_metadata = output_collection_metadata(collected, self.request, self.output_dir)
                    if not collected.passed:
                        problems.append("unsafe output files were rejected")
            else:
                problems.append(f"{self.label} container did not start")
        except Exception as exc:
            trajectory = None
            problems.append(self.redact(str(exc)))
        finally:
            if self.proxy is not None:
                try:
                    self.proxy.stop()
                    if self.proxy.log_path.exists():
                        redact_jsonl_file(self.proxy.log_path)
                except Exception as exc:
                    problems.append(self.redact(str(exc)))
            if self.capture is not None:
                if trace is not None:
                    try:
                        self.capture.add_native(trace, self.native_trace_name)
                    except OSError as exc:
                        self.capture.errors.append(f"native evidence collection failed: {self.native_trace_name}: {exc}")
                self.collect_native_evidence(usage)
                if self.redact_capture_errors:
                    self.capture_metadata = self.capture.finish(status, error_formatter=lambda exc: self.redact(str(exc)))
                else:
                    self.capture_metadata = self.capture.finish(status)
            if problems and status == "ok":
                status, error = "error", "; ".join(problems)
            retained = self.container_started and config.keep_container_on_error and status != "ok"
            if self.container_started and not retained:
                try:
                    removed = remove_container(runtime, self.container)
                    if removed.returncode:
                        logger.warning("%s container removal failed: %s", self.label, self.container)
                except Exception:
                    logger.warning("%s container removal failed: %s", self.label, self.container, exc_info=True)
        elapsed = time.monotonic() - self.started
        usage["elapsed_seconds"] = round(elapsed, 3)
        provenance = build_provenance(
            self.request, harness=self.harness.name, docker_image=config.docker_image,
            docker_image_digest=self.image_digest,
            api_base_urls=[config.api_base or self.default_api_base],
            environment=self.environment, attempt=self.attempt, started_at=self.started_at,
            finished_at=datetime.now(timezone.utc).isoformat(), **self.version_fields(),
        )
        self._result = HarnessResult(
            status=status, error=self.redact(error) if error else None, elapsed_seconds=elapsed, usage=usage,
            trace_path=trace.relative_to(self.output_dir).as_posix() if trace else None,
            trajectory_path=trajectory.relative_to(self.output_dir).as_posix() if trajectory else None,
            metadata={
                **self.capture_metadata, "raw_trace_schema": self.raw_trace_schema, **self.version_fields(),
                "provenance": provenance, "output_collection": output_metadata, "turn_count": self.turns,
                **self.result_metadata(),
                "retained_container": self.container if retained else None,
                "settings": {"reasoning_effort": resolve_thinking(self.request.task.thinking, config.reasoning_effort),
                             **self.settings(), "keep_thinking": config.keep_thinking},
            },
        )
        return self._result


class BridgeHarness(InteractiveHarness):
    session_class: ClassVar[type[BridgeSession]]

    def __init__(self, config: Any, worker_slot: int, runtime: DockerRuntime | None = None):
        self.config, self.worker_slot = config, worker_slot
        self.runtime = runtime or DockerRuntime(config.docker_binary)

    def create_session(
        self, request: RunRequest, output_dir: Path, attempt: int, *,
        collect_outputs: bool = True,
    ) -> BridgeSession:
        return self.session_class(self, request, output_dir, attempt, collect_outputs=collect_outputs)

    def run(self, request: RunRequest, output_dir: Path, attempt: int) -> HarnessResult:
        session = self.create_session(request, output_dir, attempt)
        status, error = "ok", None
        try:
            session.start()
            for index, message in enumerate(request.task.prompts):
                if session.supports_reset and index < len(request.task.new_session) and request.task.new_session[index]:
                    session.reset()
                session.send(message, session.remaining())
        except (TimeoutError, subprocess.TimeoutExpired):
            status, error = "timeout", f"task exceeded {request.timeout_seconds}s"
        except Exception as exc:
            status, error = "error", session.redact(str(exc))
        return session.finish(status, error)
