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
from roller.opencode_actf import convert_opencode_trace, extract_opencode_usage
from roller.models import HarnessResult, RunRequest
from roller.storage import atomic_write_json
from roller.thinking import normalize_thinking, resolve_thinking
from roller.usage import ZERO_USAGE
from roller.version import __version__
from roller.wire_trace import WireTraceProxy, redact_jsonl_file

logger = logging.getLogger(__name__)
OPENCODE_VERSION = "1.18.30"
DEFAULT_OPENCODE_IMAGE = "roller-opencode:1.18.30"
COMMON_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
RUNNER_PATH = Path(__file__).resolve().parents[1] / "assets" / "opencode" / "runner.py"
STATE = "/roller/opencode-state"
RUNNER = "/roller/opencode_runner.py"
PLUGIN = "/roller/opencode_journal.mjs"

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LOCAL_PROXY_BYPASS = ("localhost", "127.0.0.1", "::1")


def _ensure_local_proxy_bypass(environment: dict[str, str]) -> None:
    """Keep benchmark-local HTTP services out of inherited public proxies."""

    configured: list[str] = []
    for name in ("NO_PROXY", "no_proxy"):
        configured.extend(
            item.strip()
            for item in environment.get(name, "").split(",")
            if item.strip()
        )
    value = ",".join(dict.fromkeys((*LOCAL_PROXY_BYPASS, *configured)))
    environment["NO_PROXY"] = value
    environment["no_proxy"] = value


def _system_prompt(request: RunRequest, workspace: str) -> str:
    bench = request.task.raw.get("bench")
    bench_name = bench.get("name") if isinstance(bench, dict) else None
    if bench_name == "skillsbench":
        delivery = "Follow the task's exact requested output paths."
    else:
        delivery = f"Save deliverables under {request.task.output_dir}."
    context = ", ".join(item.target for item in request.task.context_files)
    return (
        f"You are running a Roller task. Work in {workspace}. "
        f"Task inputs are in the workspace. {delivery} "
        f"Task context files (read when relevant): {context or '(none)'}. "
        "Complete the requested task and return a concise final answer."
    )


def opencode_effort(value: str | None) -> str | None:
    value = normalize_thinking(value, field="OpenCode reasoning effort")
    if value not in {None, "off", "minimal", "low", "medium", "high", "xhigh"}:
        raise ValueError("OpenCode reasoning effort must be off|minimal|low|medium|high|xhigh; provider support is required")
    return value


@dataclass(frozen=True)
class OpenCodeConfig:
    docker_image: str = DEFAULT_OPENCODE_IMAGE
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
    vision: bool = True
    deny_tools: tuple[str, ...] = ()
    wire_trace_mode: str = "actf"
    keep_thinking: bool = True
    keep_container_on_error: bool = False
    docker_binary: str = "docker"

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasoning_effort", opencode_effort(self.reasoning_effort))
        if any(not isinstance(name, str) or not name.strip() for name in self.deny_tools):
            raise ValueError("OpenCode denied tools must contain non-empty names")
        if not self.docker_image.strip():
            raise ValueError("OpenCode docker image must be non-empty")
        if self.container_user is not None and not self.container_user.strip():
            raise ValueError("OpenCode container user must be non-empty or None")
        if not self.workspace_container_path.startswith("/"):
            raise ValueError("OpenCode workspace container path must be absolute")
        if not all(ENV_NAME.fullmatch(name) for name in (self.api_key_env, *self.pass_env)):
            raise ValueError("invalid OpenCode environment variable name")
        if isinstance(self.max_steps, bool) or not isinstance(self.max_steps, int) or self.max_steps < 1:
            raise ValueError("OpenCode max steps must be a positive integer")
        if (isinstance(self.context_length, bool) or not isinstance(self.context_length, int) or self.context_length < 1024):
            raise ValueError("OpenCode context length must be at least 1024 tokens")
        if isinstance(self.output_tokens, bool) or not isinstance(self.output_tokens, int) or not 1 <= self.output_tokens < self.context_length:
            raise ValueError("OpenCode output tokens must be positive and smaller than context length")
        if self.api_base:
            try:
                parsed = urlsplit(self.api_base)
                parsed.port
                valid = parsed.scheme in {"http", "https"} and parsed.hostname and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("OpenCode API base must be an absolute http(s) URL without credentials, query or fragment")
        if self.wire_trace_mode not in {"off", "actf", "full"}:
            raise ValueError("OpenCode wire trace mode must be off|actf|full")
        if self.wire_trace_mode == "full" and (not self.api_base or self.network != "host"):
            raise ValueError("OpenCode --wire-trace full requires --api-base and --network host")


class OpenCodeHarness(InteractiveHarness):
    """Run the pinned OpenCode CLI in an isolated task Docker container."""

    name = "opencode"

    def __init__(self, config: OpenCodeConfig, worker_slot: int, runtime: DockerRuntime | None = None):
        self.config, self.worker_slot = config, worker_slot
        self.runtime = runtime or DockerRuntime(config.docker_binary)

    def create_session(
        self, request: RunRequest, output_dir: Path, attempt: int, *,
        collect_outputs: bool = True,
    ) -> OpenCodeSession:
        return OpenCodeSession(self, request, output_dir, attempt, collect_outputs=collect_outputs)

    def run(self, request: RunRequest, output_dir: Path, attempt: int) -> HarnessResult:
        session = self.create_session(request, output_dir, attempt)
        status, error = "ok", None
        try:
            session.start()
            for index, message in enumerate(request.task.prompts):
                if index < len(request.task.new_session) and request.task.new_session[index]:
                    session.reset()
                session.send(message, session.remaining())
        except (TimeoutError, subprocess.TimeoutExpired):
            status, error = "timeout", f"task exceeded {request.timeout_seconds}s"
        except Exception as exc:
            status, error = "error", session.redact(str(exc))
        return session.finish(status, error)


class OpenCodeSession(InteractiveSession):
    def __init__(
        self, harness: OpenCodeHarness, request: RunRequest, output_dir: Path,
        attempt: int, *, collect_outputs: bool = True,
    ) -> None:
        self.harness, self.request = harness, request
        self.output_dir, self.attempt = Path(output_dir), attempt
        self.collect_outputs = collect_outputs
        self.container = f"roller-opencode-{request.index}-{uuid.uuid4().hex[:12]}"
        self.container_started = False
        self.transcript_container_path = STATE + "/trace.jsonl"
        self.started = time.monotonic()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.environment = {}
        self.image_digest = None
        self.opencode_version = None
        self.proxy = None
        self._result: HarnessResult | None = None
        self.turns = 0

    def remaining(self) -> float:
        remaining = self.request.timeout_seconds - (time.monotonic() - self.started)
        if remaining <= 0:
            raise TimeoutError("OpenCode task timed out")
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
            raise RuntimeError("OpenCode session has already started or finished")
        config, task, bundle = self.harness.config, self.request.task, self.request.bundle
        opencode_effort(resolve_thinking(task.thinking, config.reasoning_effort))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        environment = {name: os.environ[name] for name in config.pass_env if os.environ.get(name)}
        _ensure_local_proxy_bypass(environment)
        for name in task.env_names:
            if not ENV_NAME.fullmatch(name) or not os.environ.get(name):
                raise ValueError(f"task requires missing or invalid environment variable: {name}")
            environment[name] = os.environ[name]
        if not os.environ.get(config.api_key_env):
            raise ValueError(f"OpenCode requires a non-empty API key in {config.api_key_env}")
        environment["OPENAI_API_KEY"] = os.environ[config.api_key_env]
        api_base = config.api_base or "https://api.openai.com/v1"
        if config.wire_trace_mode == "full":
            self.proxy = WireTraceProxy(api_base, self.output_dir / "wire_trace.jsonl", mode="full")
            self.proxy.start()
            api_base = self.proxy.proxy_base_url
        environment.update(
            # Preserve the task image's HOME. Benchmark environments commonly
            # install browser/tool caches under /root and verifier scripts use
            # $HOME to find them. OpenCode state remains isolated through the
            # explicit XDG and OPENCODE_CONFIG paths below.
            OPENAI_BASE_URL=api_base,
            XDG_CONFIG_HOME="/roller/opencode-home/config",
            XDG_DATA_HOME="/roller/opencode-home/data",
            XDG_STATE_HOME="/roller/opencode-home/state",
            OPENCODE_CONFIG=STATE + "/opencode.json",
            OPENCODE_DISABLE_PROJECT_CONFIG="1", OPENCODE_DISABLE_MODELS_FETCH="1",
            OPENCODE_DISABLE_AUTOUPDATE="1", OPENCODE_DISABLE_PRUNE="1",
            OPENCODE_DISABLE_AUTOCOMPACT="1", OPENCODE_DISABLE_FFF="1",
            OPENCODE_DISABLE_DEFAULT_PLUGINS="1",
            ROLLER_OPENCODE_JOURNAL=STATE + "/trace.jsonl",
        )
        self.environment = environment
        self.image_digest = self.checked(["image", "inspect", config.docker_image, "--format", "{{.Id}}"], "OpenCode image inspection").stdout.strip()
        args = ["run", "-d", "--name", self.container, "--label", "ai.roller.harness=opencode"]
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
        self.checked(args, "OpenCode container startup")
        workspace = shlex.quote(config.workspace_container_path)
        output = shlex.quote(f"{config.workspace_container_path}/{task.output_dir}")
        script = f"mkdir -p {workspace} {STATE} /roller/opencode-home /roller/task"
        script += " && mkdir -p /roller/opencode-home/config/opencode"
        script += " && test -d /opt/roller-opencode-deps/node_modules"
        script += " && cp /opt/roller-opencode-deps/package*.json /roller/opencode-home/config/opencode/"
        script += " && ln -s /opt/roller-opencode-deps/node_modules /roller/opencode-home/config/opencode/node_modules"
        if bundle and (bundle.legacy_workspace or task.input_mode == "copy"):
            script += f" && cp -a /roller/task/inputs/. {workspace}/ && chmod -R u+w {workspace}"
        elif task.workspace and not bundle:
            script += f" && cp -a /roller-input/. {workspace}/ && chmod -R u+w {workspace}"
        if bundle:
            script += f" && cp -a /roller/task/context/. {workspace}/"
        script += f" && rm -rf {output} && mkdir -p {output} && ln -s {output} /roller/task/outputs"
        self.checked(["exec", self.container, "/bin/bash", "-c", script], "OpenCode workspace setup")
        self.checked(["cp", str(RUNNER_PATH), f"{self.container}:{RUNNER}"], "OpenCode runner copy")
        self.checked(["cp", str(RUNNER_PATH.with_name("journal.mjs")), f"{self.container}:{PLUGIN}"], "OpenCode journal plugin copy")
        for command in task.setup_commands:
            self.checked(["exec", "-w", config.workspace_container_path, self.container, "/bin/bash", "-c", command], "task setup", timeout=self.remaining())
        self.opencode_version = self.checked(["exec", self.container, "python3", RUNNER, "--check"], "OpenCode version check").stdout.strip()

    def send(self, message: str, timeout_seconds: float) -> TurnResult:
        if not self.container_started or self._result is not None:
            raise RuntimeError("OpenCode session is not active")
        started = time.monotonic()
        timeout = min(timeout_seconds, self.remaining())
        if timeout <= 0:
            raise TimeoutError("OpenCode turn timed out")
        config = self.harness.config
        self.turns += 1
        # Context is present as regular workspace files for every role; OpenCode's
        # implicit host/profile discovery is disabled in the bridge.
        payload = {
            "session_id": self.container, "model": self.request.model, "message": message,
            "thinking": opencode_effort(resolve_thinking(self.request.task.thinking, config.reasoning_effort)),
            "max_steps": config.max_steps, "timeout_seconds": timeout,
            "context_length": config.context_length, "output_tokens": config.output_tokens,
            "vision": config.vision, "deny_tools": list(config.deny_tools),
            "system_prompt": _system_prompt(
                self.request, config.workspace_container_path
            ),
        }
        config_path = self.output_dir / ".opencode-turn.json"
        atomic_write_json(config_path, payload)
        log_path = self.output_dir / "rollout" / f"opencode.attempt-{self.attempt}.log"
        process = None
        try:
            self.checked(["cp", str(config_path), f"{self.container}:{STATE}/turn.json"], "OpenCode turn configuration")
            args = ["exec", "-w", config.workspace_container_path, self.container, "python3", RUNNER, "--config", STATE + "/turn.json"]
            process = self.harness.runtime.popen(args, log_path, append=True)
            try:
                return_code = process.wait(timeout=max(0.001, timeout - (time.monotonic() - started)))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                # Stop the container's exec process before collecting partial artifacts.
                self.harness.runtime.run(["kill", self.container], timeout=30)
                raise TimeoutError("OpenCode turn timed out") from None
            response = self.checked(["exec", self.container, "cat", STATE + "/result.json"], "OpenCode turn result")
            result = json.loads(response.stdout)
            if return_code or result.get("ok") is not True or not isinstance(result.get("content"), str) or not result["content"].strip():
                raise RuntimeError(self.redact(str(result.get("error") or f"OpenCode runner exited with code {return_code}")))
            return TurnResult(result["content"], time.monotonic() - started, return_code)
        finally:
            config_path.unlink(missing_ok=True)
            self.harness.runtime.close_log(process)
            if log_path.exists():
                log_path.write_text(self.redact(log_path.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")

    def reset(self) -> None:
        """Forget conversation state without touching the task workspace."""

        if not self.container_started or self._result is not None:
            raise RuntimeError("OpenCode session is not active")
        self.checked(
            ["exec", self.container, "rm", "-f", STATE + "/session.json"],
            "OpenCode session reset",
        )

    def finish(self, status: str, error: str | None = None) -> HarnessResult:
        if self._result is not None:
            return self._result
        config, runtime = self.harness.config, self.harness.runtime
        trace = trajectory = None
        output_metadata = None
        usage = dict(ZERO_USAGE)
        problems = []
        warnings = []
        trajectory_conversion = None
        try:
            if self.container_started:
                try:
                    trace = self.output_dir / "rollout" / "trace.jsonl"
                    trace.parent.mkdir(parents=True, exist_ok=True)
                    copied = runtime.run(["cp", f"{self.container}:{self.transcript_container_path}", str(trace)], timeout=30)
                    if copied.returncode or not trace.is_file() or not trace.stat().st_size:
                        trace = None
                        problems.append("no OpenCode journal was collected")
                    else:
                        usage = extract_opencode_usage(trace)
                        trajectory = trace.with_name("trace_ACTF.json")
                        converted = convert_opencode_trace(trace, trajectory, keep_thinking=config.keep_thinking)
                        if not converted.get("steps"):
                            raise ValueError("OpenCode journal produced no assistant steps")
                except Exception as exc:
                    trajectory = None
                    # The native journal is still retained and benchmark
                    # grading has already observed the task workspace. Failure
                    # to derive a text-only training trajectory must not turn a
                    # valid evaluation sample into an agent error.
                    conversion_error = self.redact(str(exc))
                    warnings.append(
                        "OpenCode trace conversion failed: " + conversion_error
                    )
                    if trace is not None and trace.is_file() and trace.stat().st_size:
                        trajectory_conversion = {
                            "status": "unavailable",
                            "target": "actf",
                            "error": conversion_error,
                        }
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
                problems.append("OpenCode container did not start")
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
                        logger.warning("OpenCode container removal failed: %s", self.container)
                except Exception:
                    logger.warning("OpenCode container removal failed: %s", self.container, exc_info=True)
        elapsed = time.monotonic() - self.started
        usage["elapsed_seconds"] = round(elapsed, 3)
        bundle = self.request.bundle
        provenance = {
            "schema_version": "roller.provenance/v1", "roller_version": __version__,
            "task_id": self.request.task.id, "harness": "opencode", "model": self.request.model,
            "opencode_version": self.opencode_version,
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
            metadata={"raw_trace_schema": "roller.opencode-events/v1", "opencode_version": self.opencode_version,
                "provenance": provenance, "output_collection": output_metadata, "turn_count": self.turns,
                "warnings": warnings,
                "trajectory_conversion": trajectory_conversion,
                "retained_container": self.container if retained else None,
                "settings": {"reasoning_effort": resolve_thinking(self.request.task.thinking, config.reasoning_effort), "max_steps": config.max_steps, "context_length": config.context_length, "output_tokens": config.output_tokens, "vision": config.vision, "deny_tools": list(config.deny_tools), "tool_policy": "native", "keep_thinking": config.keep_thinking}},
        )
        return self._result
