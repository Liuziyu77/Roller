from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
from typing import Sequence
from urllib.parse import urlsplit
import uuid

from roller.capture_runtime import CaptureRuntime
from roller.artifacts import collect_output_directory
from roller.harnesses.base import InteractiveHarness, InteractiveSession, TurnResult
from roller.harnesses.docker import DockerRuntime, remove_container
from roller.pi_actf import convert_pi_trace, extract_pi_usage
from roller.models import HarnessResult, RunRequest
from roller.storage import atomic_write_json
from roller.thinking import normalize_thinking, resolve_thinking
from roller.usage import ZERO_USAGE
from roller.version import __version__
from roller.wire_trace import WireTraceProxy, redact_jsonl_file

logger = logging.getLogger(__name__)
PI_VERSION = "0.85.1"
DEFAULT_PI_IMAGE = "roller-pi:0.85.1"
COMMON_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
RUNNER_PATH = Path(__file__).resolve().parents[1] / "assets" / "pi" / "runner.py"
STATE = "/roller/pi-state"
RUNNER = "/roller/pi_runner.py"
SDK_RUNNER = "/roller/pi_sdk.mjs"
PI_TOOLS = ("read", "bash", "edit", "write", "grep", "find", "ls")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def pi_effort(value: str | None) -> str | None:
    value = normalize_thinking(value, field="Pi reasoning effort")
    if value not in {None, "off", "minimal", "low", "medium", "high", "xhigh", "max"}:
        raise ValueError("Pi reasoning effort must be off|minimal|low|medium|high|xhigh|max; provider support is required")
    return value


@dataclass(frozen=True)
class PiConfig:
    docker_image: str = DEFAULT_PI_IMAGE
    container_user: str | None = "0"
    network: str | None = "host"
    workspace_container_path: str = "/roller/workspace"
    api_base: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    pass_env: tuple[str, ...] = COMMON_ENV_NAMES
    reasoning_effort: str | None = None
    max_steps: int = 12
    context_length: int = 128000
    output_tokens: int = 8192
    capture_calls: bool = False
    capture_debug: str = "on-error"
    wire_trace_mode: str = "actf"
    keep_thinking: bool = True
    keep_container_on_error: bool = False
    docker_binary: str = "docker"

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasoning_effort", pi_effort(self.reasoning_effort) or "off")
        if not self.docker_image.strip():
            raise ValueError("Pi docker image must be non-empty")
        if self.container_user is not None and not self.container_user.strip():
            raise ValueError("Pi container user must be non-empty or None")
        if not self.workspace_container_path.startswith("/"):
            raise ValueError("Pi workspace container path must be absolute")
        if not all(ENV_NAME.fullmatch(name) for name in (self.api_key_env, *self.pass_env)):
            raise ValueError("invalid Pi environment variable name")
        if isinstance(self.max_steps, bool) or not isinstance(self.max_steps, int) or self.max_steps < 1:
            raise ValueError("Pi max steps must be a positive integer")
        if (isinstance(self.context_length, bool) or not isinstance(self.context_length, int) or self.context_length < 1024):
            raise ValueError("Pi context length must be at least 1024 tokens")
        if isinstance(self.output_tokens, bool) or not isinstance(self.output_tokens, int) or not 1 <= self.output_tokens < self.context_length:
            raise ValueError("Pi output tokens must be positive and smaller than context length")
        if self.api_base:
            try:
                parsed = urlsplit(self.api_base)
                parsed.port
                valid = parsed.scheme in {"http", "https"} and parsed.hostname and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("Pi API base must be an absolute http(s) URL without credentials, query or fragment")
        if self.capture_calls and (not self.api_base or self.network != "host" or self.wire_trace_mode == "off"):
            raise ValueError("pi call capture requires --api-base, --network host and wire tracing")
        if self.wire_trace_mode not in {"off", "actf", "full"}:
            raise ValueError("Pi wire trace mode must be off|actf|full")
        if self.wire_trace_mode == "full" and (not self.api_base or self.network != "host"):
            raise ValueError("Pi --wire-trace full requires --api-base and --network host")


class PiHarness(InteractiveHarness):
    """Run the official Pi AgentSession and tools in a task Docker container."""

    name = "pi"

    def __init__(self, config: PiConfig, worker_slot: int, runtime: DockerRuntime | None = None):
        self.config, self.worker_slot = config, worker_slot
        self.runtime = runtime or DockerRuntime(config.docker_binary)

    def create_session(
        self, request: RunRequest, output_dir: Path, attempt: int, *,
        collect_outputs: bool = True,
    ) -> PiSession:
        return PiSession(self, request, output_dir, attempt, collect_outputs=collect_outputs)

    def run(self, request: RunRequest, output_dir: Path, attempt: int) -> HarnessResult:
        session = self.create_session(request, output_dir, attempt)
        status, error = "ok", None
        try:
            session.start()
            for message in request.task.prompts:
                session.send(message, session.remaining())
        except (TimeoutError, subprocess.TimeoutExpired):
            status, error = "timeout", f"task exceeded {request.timeout_seconds}s"
        except Exception as exc:
            status, error = "error", session.redact(str(exc))
        return session.finish(status, error)


class PiSession(InteractiveSession):
    def __init__(
        self, harness: PiHarness, request: RunRequest, output_dir: Path,
        attempt: int, *, collect_outputs: bool = True,
    ) -> None:
        self.harness, self.request = harness, request
        self.output_dir, self.attempt = Path(output_dir), attempt
        self.collect_outputs = collect_outputs
        self.container = f"roller-pi-{request.index}-{uuid.uuid4().hex[:12]}"
        self.container_started = False
        self.transcript_container_path = STATE + "/trace.jsonl"
        self.started = time.monotonic()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.environment = {}
        self.image_digest = None
        self.pi_version = None
        self.proxy = None
        self.capture = CaptureRuntime(self.output_dir, attempt, "pi", harness.config.capture_debug) if harness.config.capture_calls else None
        self.capture_metadata = {}
        self._result: HarnessResult | None = None
        self.turns = 0

    def remaining(self) -> float:
        remaining = self.request.timeout_seconds - (time.monotonic() - self.started)
        if remaining <= 0:
            raise TimeoutError("Pi task timed out")
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
            raise RuntimeError("Pi session has already started or finished")
        config, task, bundle = self.harness.config, self.request.task, self.request.bundle
        pi_effort(resolve_thinking(task.thinking, config.reasoning_effort))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        environment = {name: os.environ[name] for name in config.pass_env if os.environ.get(name)}
        for name in task.env_names:
            if not ENV_NAME.fullmatch(name) or not os.environ.get(name):
                raise ValueError(f"task requires missing or invalid environment variable: {name}")
            environment[name] = os.environ[name]
        if not os.environ.get(config.api_key_env):
            raise ValueError(f"Pi requires a non-empty API key in {config.api_key_env}")
        environment["PI_API_KEY"] = os.environ[config.api_key_env]
        api_base = config.api_base or "https://api.openai.com/v1"
        if config.wire_trace_mode == "full" or config.capture_calls:
            self.proxy = WireTraceProxy(api_base, self.output_dir / "wire_trace.jsonl", mode="full",
                capture_recorder=self.capture.recorder if self.capture else None,
                capture_identity_header="x-roller-native-session-id" if self.capture else None)
            self.proxy.start()
            api_base = self.proxy.proxy_base_url
        environment.update(
            PI_BASE_URL=api_base, HOME="/roller/pi-home",
            PI_CODING_AGENT_DIR="/roller/pi-home/agent", PI_OFFLINE="1",
        )
        self.environment = environment
        self.image_digest = self.checked(["image", "inspect", config.docker_image, "--format", "{{.Id}}"], "Pi image inspection").stdout.strip()
        args = ["run", "-d", "--name", self.container, "--label", "ai.roller.harness=pi"]
        if config.container_user is not None:
            args += ["--user", config.container_user]
        if config.network:
            args += ["--network", config.network]
        for name, value in sorted(environment.items()):
            args += ["-e", f"{name}={value}"]
        if bundle:
            args += ["-v", f"{bundle.inputs_root}:/roller/task/inputs:ro", "-v", f"{bundle.context_root}:/roller/task/context:ro"]
            if not bundle.legacy_workspace and task.input_mode == "mount":
                for spec in task.inputs:
                    source = bundle.inputs_root / spec.path
                    if source.exists():
                        args += ["-v", f"{source}:{config.workspace_container_path}/{spec.path}:ro"]
        elif task.workspace:
            if not task.workspace.is_dir():
                raise FileNotFoundError(f"task workspace is not a directory: {task.workspace}")
            args += ["-v", f"{task.workspace}:/roller-input:ro"]
        args += ["--entrypoint", "/bin/bash", config.docker_image, "-c", "tail -f /dev/null"]
        # Mark before run: a timed-out Docker client may already have created it.
        self.container_started = True
        self.checked(args, "Pi container startup")
        workspace = shlex.quote(config.workspace_container_path)
        output = shlex.quote(f"{config.workspace_container_path}/{task.output_dir}")
        script = f"mkdir -p {workspace} {STATE} /roller/pi-home /roller/task"
        if bundle and (bundle.legacy_workspace or task.input_mode == "copy"):
            script += f" && cp -a /roller/task/inputs/. {workspace}/ && chmod -R u+w {workspace}"
        elif task.workspace and not bundle:
            script += f" && cp -a /roller-input/. {workspace}/ && chmod -R u+w {workspace}"
        if bundle:
            script += f" && cp -a /roller/task/context/. {workspace}/"
        script += f" && rm -rf {output} && mkdir -p {output} && ln -s {output} /roller/task/outputs"
        self.checked(["exec", self.container, "/bin/bash", "-c", script], "Pi workspace setup")
        self.checked(["cp", str(RUNNER_PATH), f"{self.container}:{RUNNER}"], "Pi runner copy")
        self.checked(["cp", str(RUNNER_PATH.with_name("runner.mjs")), f"{self.container}:{SDK_RUNNER}"], "Pi SDK runner copy")
        for command in task.setup_commands:
            self.checked(["exec", "-w", config.workspace_container_path, self.container, "/bin/bash", "-c", command], "task setup", timeout=self.remaining())
        self.pi_version = self.checked(["exec", self.container, "python3", RUNNER, "--check"], "Pi version check").stdout.strip()

    def send(self, message: str, timeout_seconds: float) -> TurnResult:
        if not self.container_started or self._result is not None:
            raise RuntimeError("Pi session is not active")
        started = time.monotonic()
        timeout = min(timeout_seconds, self.remaining())
        if timeout <= 0:
            raise TimeoutError("Pi turn timed out")
        config = self.harness.config
        self.turns += 1
        # Pass explicit Context paths while keeping Pi resource discovery isolated.
        context = ", ".join(item.target for item in self.request.task.context_files)
        payload = {
            "capture_calls": config.capture_calls, "session_id": self.container, "turn": self.turns, "model": self.request.model, "message": message,
            "thinking": pi_effort(resolve_thinking(self.request.task.thinking, config.reasoning_effort)),
            "max_steps": config.max_steps, "timeout_seconds": timeout,
            "context_length": config.context_length, "output_tokens": config.output_tokens,
            "tools": list(PI_TOOLS),
            "context_paths": [item.target for item in self.request.task.context_files if item.role != "skill"],
            "system_prompt": f"You are running a Roller task. Work in {config.workspace_container_path}. "
                f"Task inputs are in the workspace. Save deliverables under {self.request.task.output_dir}. "
                f"Task context files (read when relevant): {context or '(none)'}. "
                "Complete the requested task and return a concise final answer.",
        }
        config_path = self.output_dir / ".pi-turn.json"
        atomic_write_json(config_path, payload)
        log_path = self.output_dir / "rollout" / f"pi.attempt-{self.attempt}.log"
        process = None
        try:
            self.checked(["cp", str(config_path), f"{self.container}:{STATE}/turn.json"], "Pi turn configuration")
            args = ["exec", "-w", config.workspace_container_path, self.container, "python3", RUNNER, "--config", STATE + "/turn.json"]
            process = self.harness.runtime.popen(args, log_path, append=True)
            try:
                return_code = process.wait(timeout=max(0.001, timeout - (time.monotonic() - started)))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                # Stop the container's exec process before collecting partial artifacts.
                self.harness.runtime.run(["kill", self.container], timeout=30)
                raise TimeoutError("Pi turn timed out") from None
            response = self.checked(["exec", self.container, "cat", STATE + "/result.json"], "Pi turn result")
            result = json.loads(response.stdout)
            if result.get("timeout") is True:
                self.harness.runtime.run(["kill", self.container], timeout=30)
                raise TimeoutError("Pi turn timed out")
            if return_code or result.get("ok") is not True or not isinstance(result.get("content"), str) or not result["content"].strip():
                raise RuntimeError(self.redact(str(result.get("error") or f"Pi runner exited with code {return_code}")))
            return TurnResult(result["content"], time.monotonic() - started, return_code)
        finally:
            config_path.unlink(missing_ok=True)
            self.harness.runtime.close_log(process)
            if log_path.exists():
                log_path.write_text(self.redact(log_path.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")

    def finish(self, status: str, error: str | None = None) -> HarnessResult:
        if self._result is not None:
            return self._result
        config, runtime = self.harness.config, self.harness.runtime
        trace = trajectory = None
        output_metadata = None
        usage = dict(ZERO_USAGE)
        problems = []
        try:
            if self.container_started:
                # Startup/import failures may precede the native journal.
                try:
                    log = self.output_dir / "rollout" / f"pi.runtime.attempt-{self.attempt}.log"
                    log.parent.mkdir(parents=True, exist_ok=True)
                    runtime.run(["cp", f"{self.container}:{STATE}/runtime.log", str(log)], timeout=30)
                    if log.is_file():
                        log.write_text(self.redact(log.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
                except Exception:
                    logger.warning("Pi runtime log collection failed", exc_info=True)
                try:
                    trace = self.output_dir / "rollout" / "trace.jsonl"
                    trace.parent.mkdir(parents=True, exist_ok=True)
                    copied = runtime.run(["cp", f"{self.container}:{self.transcript_container_path}", str(trace)], timeout=30)
                    if copied.returncode or not trace.is_file() or not trace.stat().st_size:
                        trace = None
                        problems.append("no Pi journal was collected")
                    else:
                        trace.write_text(self.redact(trace.read_text(encoding="utf-8")), encoding="utf-8")
                        usage = extract_pi_usage(trace)
                        trajectory = trace.with_name("trace_ACTF.json")
                        converted = convert_pi_trace(trace, trajectory, keep_thinking=config.keep_thinking)
                        if not converted.get("steps"):
                            raise ValueError("Pi journal produced no assistant steps")
                except Exception as exc:
                    trajectory = None
                    problems.append("Pi trace conversion failed: " + self.redact(str(exc)))
                if self.collect_outputs:
                    staging = self.output_dir / ".runtime-outputs"
                    staging.mkdir(parents=True, exist_ok=True)
                    try:
                        copied = runtime.run(["cp", f"{self.container}:{config.workspace_container_path}/{self.request.task.output_dir}/.", str(staging)], timeout=30)
                        if copied.returncode:
                            problems.append("output collection failed")
                        collected = collect_output_directory(staging, self.output_dir)
                    finally:
                        shutil.rmtree(staging, ignore_errors=True)
                    output_metadata = {"mode": "all", "agent_output_dir": self.request.task.output_dir, "passed": collected.passed, "path": collected.results_path.relative_to(self.output_dir).as_posix() if collected.results_path else None, "file_count": collected.file_count, "directory_count": collected.directory_count, "files": list(collected.files), "errors": list(collected.errors)}
                    if not collected.passed:
                        problems.append("unsafe output files were rejected")
            else:
                problems.append("Pi container did not start")
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
                    self.capture.add_native(trace)
                self.capture_metadata = self.capture.finish(status)
            if problems and status == "ok":
                status, error = "error", "; ".join(problems)
            retained = self.container_started and config.keep_container_on_error and status != "ok"
            if self.container_started and not retained:
                try:
                    removed = remove_container(runtime, self.container)
                    if removed.returncode:
                        logger.warning("Pi container removal failed: %s", self.container)
                except Exception:
                    logger.warning("Pi container removal failed: %s", self.container, exc_info=True)
        elapsed = time.monotonic() - self.started
        usage["elapsed_seconds"] = round(elapsed, 3)
        bundle = self.request.bundle
        provenance = {
            "schema_version": "roller.provenance/v1", "roller_version": __version__,
            "task_id": self.request.task.id, "harness": "pi", "model": self.request.model,
            "pi_version": self.pi_version, "api_mode": "openai-completions",
            "docker_image": config.docker_image, "docker_image_digest": self.image_digest,
            "api_base_urls": [config.api_base or "https://api.openai.com/v1"],
            "environment_names": sorted(self.environment), "attempt": self.attempt,
            "started_at": self.started_at, "finished_at": datetime.now(timezone.utc).isoformat(),
            **{name: getattr(bundle, name, None) for name in ("input_tree_digest", "input_manifest_digest", "context_manifest_digest", "resume_safe")},
            "task_bundle_digest": bundle.manifest_digest if bundle else None,
        }
        self._result = HarnessResult(
            status=status, error=self.redact(error) if error else None, elapsed_seconds=elapsed, usage=usage,
            trace_path=trace.relative_to(self.output_dir).as_posix() if trace else None,
            trajectory_path=trajectory.relative_to(self.output_dir).as_posix() if trajectory else None,
            metadata={**self.capture_metadata, "raw_trace_schema": "roller.pi-events/v1", "pi_version": self.pi_version, "api_mode": "openai-completions",
                "provenance": provenance, "output_collection": output_metadata, "turn_count": self.turns,
                "retained_container": self.container if retained else None,
                "settings": {"reasoning_effort": resolve_thinking(self.request.task.thinking, config.reasoning_effort), "max_steps": config.max_steps, "context_length": config.context_length, "output_tokens": config.output_tokens, "tools": list(PI_TOOLS), "keep_thinking": config.keep_thinking}},
        )
        return self._result
