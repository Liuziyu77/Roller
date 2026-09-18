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

from roller.artifacts import collect_output_directory
from roller.harnesses.base import InteractiveHarness, InteractiveSession, TurnResult
from roller.harnesses.docker import DockerRuntime, remove_container
from roller.hermes_actf import convert_hermes_trace, extract_hermes_usage
from roller.models import HarnessResult, RunRequest
from roller.storage import atomic_write_json
from roller.thinking import normalize_thinking, resolve_thinking
from roller.usage import ZERO_USAGE
from roller.version import __version__
from roller.wire_trace import WireTraceProxy, redact_jsonl_file

logger = logging.getLogger(__name__)
HERMES_REVISION = "d15ed4445207dda418b984e8bda0f68f48b8c6f3"
DEFAULT_HERMES_IMAGE = "roller-hermes:d15ed4445207"
COMMON_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
RUNNER_PATH = Path(__file__).resolve().parents[1] / "assets" / "hermes" / "runner.py"
STATE = "/roller/hermes-state"
RUNNER = "/roller/hermes_runner.py"
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def hermes_effort(value: str | None) -> str | None:
    value = normalize_thinking(value, field="Hermes reasoning effort")
    if value not in {None, "off", "minimal", "low", "medium", "high", "xhigh"}:
        raise ValueError("Hermes reasoning effort must be off|minimal|low|medium|high|xhigh; provider support is required")
    return value


@dataclass(frozen=True)
class HermesConfig:
    docker_image: str = DEFAULT_HERMES_IMAGE
    container_user: str | None = "0"
    network: str | None = "host"
    workspace_container_path: str = "/roller/workspace"
    api_base: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    pass_env: tuple[str, ...] = COMMON_ENV_NAMES
    reasoning_effort: str | None = None
    max_iterations: int = 12
    context_length: int | None = None
    wire_trace_mode: str = "actf"
    keep_thinking: bool = True
    keep_container_on_error: bool = False
    docker_binary: str = "docker"

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasoning_effort", hermes_effort(self.reasoning_effort))
        if not self.docker_image.strip():
            raise ValueError("Hermes docker image must be non-empty")
        if self.container_user is not None and not self.container_user.strip():
            raise ValueError("Hermes container user must be non-empty or None")
        if not self.workspace_container_path.startswith("/"):
            raise ValueError("Hermes workspace container path must be absolute")
        if not all(ENV_NAME.fullmatch(name) for name in (self.api_key_env, *self.pass_env)):
            raise ValueError("invalid Hermes environment variable name")
        if isinstance(self.max_iterations, bool) or not isinstance(self.max_iterations, int) or self.max_iterations < 1:
            raise ValueError("Hermes max iterations must be a positive integer")
        if self.context_length is not None and (isinstance(self.context_length, bool) or not isinstance(self.context_length, int) or self.context_length < 64000):
            raise ValueError("Hermes context length must be at least 64000 tokens")
        if self.api_base:
            try:
                parsed = urlsplit(self.api_base)
                parsed.port
                valid = parsed.scheme in {"http", "https"} and parsed.hostname and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("Hermes API base must be an absolute http(s) URL without credentials, query or fragment")
        if self.wire_trace_mode not in {"off", "actf", "full"}:
            raise ValueError("Hermes wire trace mode must be off|actf|full")
        if self.wire_trace_mode == "full" and (not self.api_base or self.network != "host"):
            raise ValueError("Hermes --wire-trace full requires --api-base and --network host")


class HermesHarness(InteractiveHarness):
    """Run the pinned Hermes Python agent with local tools inside task Docker."""

    name = "hermes"

    def __init__(self, config: HermesConfig, worker_slot: int, runtime: DockerRuntime | None = None):
        self.config, self.worker_slot = config, worker_slot
        self.runtime = runtime or DockerRuntime(config.docker_binary)

    def create_session(
        self, request: RunRequest, output_dir: Path, attempt: int, *,
        collect_outputs: bool = True,
    ) -> HermesSession:
        return HermesSession(self, request, output_dir, attempt, collect_outputs=collect_outputs)

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


class HermesSession(InteractiveSession):
    def __init__(
        self, harness: HermesHarness, request: RunRequest, output_dir: Path,
        attempt: int, *, collect_outputs: bool = True,
    ) -> None:
        self.harness, self.request = harness, request
        self.output_dir, self.attempt = Path(output_dir), attempt
        self.collect_outputs = collect_outputs
        self.container = f"roller-hermes-{request.index}-{uuid.uuid4().hex[:12]}"
        self.container_started = False
        self.transcript_container_path = STATE + "/trace.jsonl"
        self.started = time.monotonic()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.environment = {}
        self.image_digest = None
        self.hermes_version = None
        self.proxy = None
        self._result: HarnessResult | None = None
        self.turns = 0

    def remaining(self) -> float:
        remaining = self.request.timeout_seconds - (time.monotonic() - self.started)
        if remaining <= 0:
            raise TimeoutError("Hermes task timed out")
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
            raise RuntimeError("Hermes session has already started or finished")
        config, task, bundle = self.harness.config, self.request.task, self.request.bundle
        hermes_effort(resolve_thinking(task.thinking, config.reasoning_effort))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        environment = {name: os.environ[name] for name in config.pass_env if os.environ.get(name)}
        for name in task.env_names:
            if not ENV_NAME.fullmatch(name) or not os.environ.get(name):
                raise ValueError(f"task requires missing or invalid environment variable: {name}")
            environment[name] = os.environ[name]
        if not os.environ.get(config.api_key_env):
            raise ValueError(f"Hermes requires a non-empty API key in {config.api_key_env}")
        environment["OPENAI_API_KEY"] = os.environ[config.api_key_env]
        api_base = config.api_base or "https://api.openai.com/v1"
        if config.wire_trace_mode == "full":
            self.proxy = WireTraceProxy(api_base, self.output_dir / "wire_trace.jsonl", mode="full")
            self.proxy.start()
            api_base = self.proxy.proxy_base_url
        environment.update(OPENAI_BASE_URL=api_base, HERMES_HOME="/roller/hermes-home", HERMES_DISABLE_LAZY_INSTALLS="1", TERMINAL_ENV="local", TERMINAL_CWD=config.workspace_container_path)
        self.environment = environment
        self.image_digest = self.checked(["image", "inspect", config.docker_image, "--format", "{{.Id}}"], "Hermes image inspection").stdout.strip()
        args = ["run", "-d", "--name", self.container, "--label", "ai.roller.harness=hermes"]
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
        self.checked(args, "Hermes container startup")
        workspace = shlex.quote(config.workspace_container_path)
        output = shlex.quote(f"{config.workspace_container_path}/{task.output_dir}")
        script = f"mkdir -p {workspace} {STATE} /roller/hermes-home /roller/task"
        if bundle and (bundle.legacy_workspace or task.input_mode == "copy"):
            script += f" && cp -a /roller/task/inputs/. {workspace}/ && chmod -R u+w {workspace}"
        elif task.workspace and not bundle:
            script += f" && cp -a /roller-input/. {workspace}/ && chmod -R u+w {workspace}"
        if bundle:
            script += f" && cp -a /roller/task/context/. {workspace}/"
        script += f" && rm -rf {output} && mkdir -p {output} && ln -s {output} /roller/task/outputs"
        self.checked(["exec", self.container, "/bin/bash", "-c", script], "Hermes workspace setup")
        self.checked(["cp", str(RUNNER_PATH), f"{self.container}:{RUNNER}"], "Hermes runner copy")
        for command in task.setup_commands:
            self.checked(["exec", "-w", config.workspace_container_path, self.container, "/bin/bash", "-c", command], "task setup", timeout=self.remaining())
        self.hermes_version = self.checked(["exec", self.container, "python", RUNNER, "--check"], "Hermes revision and hook check").stdout.strip()

    def send(self, message: str, timeout_seconds: float) -> TurnResult:
        if not self.container_started or self._result is not None:
            raise RuntimeError("Hermes session is not active")
        started = time.monotonic()
        timeout = min(timeout_seconds, self.remaining())
        if timeout <= 0:
            raise TimeoutError("Hermes turn timed out")
        config = self.harness.config
        self.turns += 1
        # Context is present as regular workspace files for every role; Hermes'
        # implicit host/profile discovery is disabled in the bridge.
        context = ", ".join(item.target for item in self.request.task.context_files)
        payload = {
            "session_id": self.container, "model": self.request.model, "message": message,
            "thinking": hermes_effort(resolve_thinking(self.request.task.thinking, config.reasoning_effort)),
            "max_iterations": config.max_iterations, "timeout_seconds": timeout,
            "context_length": config.context_length,
            "system_prompt": f"You are running a Roller task. Work in {config.workspace_container_path}. "
                f"Task inputs are in the workspace. Save deliverables under {self.request.task.output_dir}. "
                f"Task context files (read when relevant): {context or '(none)'}. "
                "Complete the requested task and return a concise final answer.",
        }
        config_path = self.output_dir / ".hermes-turn.json"
        atomic_write_json(config_path, payload)
        log_path = self.output_dir / "rollout" / f"hermes.attempt-{self.attempt}.log"
        process = None
        try:
            self.checked(["cp", str(config_path), f"{self.container}:{STATE}/turn.json"], "Hermes turn configuration")
            args = ["exec", "-w", config.workspace_container_path, self.container, "python", RUNNER, "--config", STATE + "/turn.json"]
            process = self.harness.runtime.popen(args, log_path, append=True)
            try:
                return_code = process.wait(timeout=max(0.001, timeout - (time.monotonic() - started)))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                # Stop the container's exec process before collecting partial artifacts.
                self.harness.runtime.run(["kill", self.container], timeout=30)
                raise TimeoutError("Hermes turn timed out") from None
            response = self.checked(["exec", self.container, "cat", STATE + "/result.json"], "Hermes turn result")
            result = json.loads(response.stdout)
            if return_code or result.get("ok") is not True or not isinstance(result.get("content"), str) or not result["content"].strip():
                raise RuntimeError(self.redact(str(result.get("error") or f"Hermes runner exited with code {return_code}")))
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
                try:
                    trace = self.output_dir / "rollout" / "trace.jsonl"
                    trace.parent.mkdir(parents=True, exist_ok=True)
                    copied = runtime.run(["cp", f"{self.container}:{self.transcript_container_path}", str(trace)], timeout=30)
                    if copied.returncode or not trace.is_file() or not trace.stat().st_size:
                        trace = None
                        problems.append("no Hermes journal was collected")
                    else:
                        usage = extract_hermes_usage(trace)
                        trajectory = trace.with_name("trace_ACTF.json")
                        converted = convert_hermes_trace(trace, trajectory, keep_thinking=config.keep_thinking)
                        if not converted.get("steps"):
                            raise ValueError("Hermes journal produced no assistant steps")
                except Exception as exc:
                    trajectory = None
                    problems.append("Hermes trace conversion failed: " + self.redact(str(exc)))
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
                problems.append("Hermes container did not start")
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
            if problems and status == "ok":
                status, error = "error", "; ".join(problems)
            retained = self.container_started and config.keep_container_on_error and status != "ok"
            if self.container_started and not retained:
                try:
                    removed = remove_container(runtime, self.container)
                    if removed.returncode:
                        logger.warning("Hermes container removal failed: %s", self.container)
                except Exception:
                    logger.warning("Hermes container removal failed: %s", self.container, exc_info=True)
        elapsed = time.monotonic() - self.started
        usage["elapsed_seconds"] = round(elapsed, 3)
        bundle = self.request.bundle
        provenance = {
            "schema_version": "roller.provenance/v1", "roller_version": __version__,
            "task_id": self.request.task.id, "harness": "hermes", "model": self.request.model,
            "hermes_revision": HERMES_REVISION, "hermes_version": self.hermes_version,
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
            metadata={"raw_trace_schema": "roller.hermes-events/v1", "hermes_version": self.hermes_version,
                "provenance": provenance, "output_collection": output_metadata, "turn_count": self.turns,
                "retained_container": self.container if retained else None,
                "settings": {"reasoning_effort": resolve_thinking(self.request.task.thinking, config.reasoning_effort), "max_iterations": config.max_iterations, "context_length": config.context_length, "toolsets": ["terminal", "file"], "keep_thinking": config.keep_thinking}},
        )
        return self._result
