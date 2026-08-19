from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from roller.artifacts import ArtifactCollectionResult, collect_output_artifacts
from roller.harnesses.base import Harness
from roller.models import HarnessResult, RunRequest
from roller.storage import atomic_write_json, safe_component
from roller.usage import ZERO_USAGE, extract_openclaw_usage
from roller.version import __version__

logger = logging.getLogger(__name__)

INPUTS_CONTAINER_PATH = "/roller/task/inputs"
CONTEXT_CONTAINER_PATH = "/roller/task/context"
OUTPUTS_CONTAINER_PATH = "/roller/task/outputs"

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

COMMON_ENV_NAMES = (
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
    "BRAVE_API_KEY",
    "TAVILY_API_KEY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


def expand_env_placeholders(value: Any, environ: Mapping[str, str] | None = None) -> Any:
    """Expand ${NAME} in model config values without persisting expanded secrets."""

    environment = os.environ if environ is None else environ
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

    def expand_string(text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in environment or environment[name] == "":
                raise ValueError(f"model config references missing environment variable {name}")
            return environment[name]

        return pattern.sub(replace, text)

    if isinstance(value, str):
        return expand_string(value)
    if isinstance(value, list):
        return [expand_env_placeholders(item, environment) for item in value]
    if isinstance(value, dict):
        return {key: expand_env_placeholders(item, environment) for key, item in value.items()}
    return value


def load_model_config(path: Path, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid model config JSON {path}: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"model config must be a JSON object: {path}")
    return expand_env_placeholders(payload, environ)


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<invalid-url>"
    if not parsed.scheme or not parsed.netloc:
        return value.split("?", 1)[0]
    hostname = parsed.hostname or ""
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _model_base_urls(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"baseurl", "base_url", "apiurl", "api_url"} and isinstance(item, str):
                urls.append(_redact_url(item))
            else:
                urls.extend(_model_base_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_model_base_urls(item))
    return list(dict.fromkeys(urls))


class DockerRuntime:
    """Small subprocess boundary that is replaceable in tests."""

    def __init__(self, binary: str = "docker") -> None:
        self.binary = binary

    def run(self, args: Sequence[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def popen(self, args: Sequence[str], log_path: Path, append: bool = False) -> subprocess.Popen[str]:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("a" if append else "w", encoding="utf-8")
        process = subprocess.Popen(
            [self.binary, *args],
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


@dataclass(frozen=True)
class OpenClawConfig:
    docker_image: str = "wildclawbench-ubuntu:v1.3"
    gateway_port_start: int = 18789
    gateway_port_step: int = 10
    gateway_startup_timeout: float = 30.0
    network: str = "host"
    workspace_container_path: str = "/tmp_workspace"
    session_id: str = "roller"
    model_config: Mapping[str, Any] | None = None
    pass_env: tuple[str, ...] = COMMON_ENV_NAMES
    search_provider: str | None = None
    thinking: str | None = None
    collect_paths: tuple[str, ...] = ()
    keep_container_on_error: bool = False
    set_image_model: bool = True
    docker_binary: str = "docker"

    def __post_init__(self) -> None:
        if not self.docker_image.strip():
            raise ValueError("docker_image cannot be empty")
        if not (1 <= self.gateway_port_start <= 65535):
            raise ValueError("gateway_port_start must be between 1 and 65535")
        if self.gateway_port_step < 1:
            raise ValueError("gateway_port_step must be positive")
        if self.gateway_startup_timeout <= 0:
            raise ValueError("gateway_startup_timeout must be positive")
        if not self.workspace_container_path.startswith("/"):
            raise ValueError("workspace_container_path must be absolute")
        if not self.session_id or "/" in self.session_id:
            raise ValueError("session_id must be a non-empty path-safe value")
        for path in self.collect_paths:
            if not path.startswith("/"):
                raise ValueError(f"collected container path must be absolute: {path}")


class OpenClawHarness(Harness):
    """Run OpenClaw in one disposable Docker container per trajectory."""

    name = "openclaw"

    _MODEL_INJECT_SCRIPT = """
import json
from pathlib import Path
config_path = Path('/root/.openclaw/openclaw.json')
models_path = Path('/tmp/roller-models.json')
config = json.loads(config_path.read_text()) if config_path.exists() else {}
config['models'] = json.loads(models_path.read_text())
config_path.parent.mkdir(parents=True, exist_ok=True)
config_path.write_text(json.dumps(config, indent=2))
""".strip()

    _AUTH_INJECT_SCRIPT = """
import json
import os
from pathlib import Path
path = Path('/root/.openclaw/agents/main/agent/auth-profiles.json')
data = json.loads(path.read_text()) if path.exists() else {'version': 1, 'profiles': {}}
data.setdefault('profiles', {})['openrouter:default'] = {
    'type': 'api_key', 'provider': 'openrouter', 'key': os.environ['ROLLER_OPENROUTER_KEY']
}
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(data, indent=2))
""".strip()

    _PERSONA_SCRIPT = """
import os
import shutil
from pathlib import Path
workspace = Path(os.environ['ROLLER_WORKSPACE']).resolve()
persona = (workspace / os.environ['ROLLER_PERSONA']).resolve()
persona.relative_to(workspace)
if persona.is_dir():
    for name in ('USER.md', 'MEMORY.md'):
        source = persona / name
        if source.is_file():
            shutil.copyfile(source, Path('/root') / name)
""".strip()

    _LATEST_SESSION_SCRIPT = """
from pathlib import Path
root = Path('/root/.openclaw/agents/main/sessions')
files = sorted(root.glob('*.jsonl'), key=lambda path: path.stat().st_mtime, reverse=True)
print(files[0] if files else '')
""".strip()

    def __init__(
        self,
        config: OpenClawConfig,
        worker_slot: int,
        runtime: DockerRuntime | None = None,
    ) -> None:
        self.config = config
        self.worker_slot = worker_slot
        self.gateway_port = config.gateway_port_start + worker_slot * config.gateway_port_step
        if self.gateway_port > 65535:
            raise ValueError(f"worker slot {worker_slot} produces invalid gateway port {self.gateway_port}")
        self.runtime = runtime or DockerRuntime(config.docker_binary)

    @staticmethod
    def _error_text(result: subprocess.CompletedProcess[str]) -> str:
        text = (result.stderr or result.stdout or "unknown error").strip()
        return text[-2000:]

    def _checked(
        self,
        args: Sequence[str],
        description: str,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = self.runtime.run(args, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"{description} failed: {self._error_text(result)}")
        return result

    def _environment(self, request: RunRequest) -> dict[str, str]:
        environment: dict[str, str] = {}
        for name in self.config.pass_env:
            if not ENV_NAME_RE.fullmatch(name):
                raise ValueError(f"invalid environment variable name: {name!r}")
            if os.environ.get(name):
                environment[name] = os.environ[name]
        for name in request.task.env_names:
            if not ENV_NAME_RE.fullmatch(name):
                raise ValueError(f"invalid task environment variable name: {name!r}")
            if name not in os.environ or os.environ[name] == "":
                raise ValueError(f"task {request.task.id!r} requires missing environment variable {name}")
            environment[name] = os.environ[name]

        # Compatibility with Interns2_data's host-side variable name.
        if "OPENROUTER_BASE_URL" not in environment and os.environ.get("OPENROUTER_BASE_URL_OPENCLAW"):
            environment["OPENROUTER_BASE_URL"] = os.environ["OPENROUTER_BASE_URL_OPENCLAW"]
        return environment

    def _start_container(self, container: str, request: RunRequest) -> dict[str, str]:
        environment = self._environment(request)
        args = [
            "run",
            "-d",
            "--name",
            container,
            "--label",
            "ai.roller.harness=openclaw",
        ]
        if self.config.network:
            args.extend(["--network", self.config.network])
        for key, value in sorted(environment.items()):
            args.extend(["-e", f"{key}={value}"])

        if request.bundle is not None:
            args.extend(["-v", f"{request.bundle.inputs_root}:{INPUTS_CONTAINER_PATH}:ro"])
            args.extend(["-v", f"{request.bundle.context_root}:{CONTEXT_CONTAINER_PATH}:ro"])
            if not request.bundle.legacy_workspace:
                args.extend(
                    [
                        "-v",
                        f"{request.bundle.inputs_root}:"
                        f"{self.config.workspace_container_path}/inputs:ro",
                    ]
                )
                args.extend(
                    [
                        "-v",
                        f"{request.bundle.context_root}:"
                        f"{self.config.workspace_container_path}/context:ro",
                    ]
                )
        else:
            workspace = request.task.workspace
            if workspace is not None:
                if not workspace.is_dir():
                    raise FileNotFoundError(f"task workspace is not a directory: {workspace}")
                args.extend(["-v", f"{workspace}:/roller-input:ro"])
        args.extend([self.config.docker_image, "/bin/bash", "-c", "tail -f /dev/null"])
        self._checked(args, "container startup", timeout=60)
        return environment

    def _prepare_workspace(self, container: str, request: RunRequest) -> None:
        workspace = self.config.workspace_container_path
        if request.bundle is not None:
            if request.bundle.legacy_workspace:
                setup = (
                    f"mkdir -p {workspace} && cp -a {INPUTS_CONTAINER_PATH}/. {workspace}/ && "
                    f"chmod -R u+w {workspace} && rm -rf {workspace}/context && "
                    f"ln -s {CONTEXT_CONTAINER_PATH} {workspace}/context"
                )
            else:
                setup = f"mkdir -p {workspace}"
        elif request.task.workspace is not None:
            setup = (
                f"mkdir -p {workspace} && cp -a /roller-input/. {workspace}/ && "
                f"chmod -R u+w {workspace}"
            )
        else:
            setup = f"mkdir -p {workspace}"
        script = (
            f"{setup} && rm -rf {workspace}/outputs && mkdir -p {workspace}/outputs && "
            f"mkdir -p /roller/task && rm -rf {OUTPUTS_CONTAINER_PATH} && "
            f"ln -s {workspace}/outputs {OUTPUTS_CONTAINER_PATH}"
        )
        self._checked(["exec", container, "/bin/bash", "-c", script], "workspace setup")
        link_script = (
            "rm -rf /root/.openclaw/workspace && "
            f"ln -s {workspace} /root/.openclaw/workspace"
        )
        self._checked(["exec", container, "/bin/bash", "-c", link_script], "OpenClaw workspace link")

    def _inject_models(self, container: str) -> None:
        if self.config.model_config is None:
            return
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", suffix=".json", delete=False
            ) as stream:
                json.dump(self.config.model_config, stream, ensure_ascii=False)
                temporary_path = Path(stream.name)
            self._checked(
                ["cp", str(temporary_path), f"{container}:/tmp/roller-models.json"],
                "model config copy",
            )
            self._checked(
                ["exec", container, "python3", "-c", self._MODEL_INJECT_SCRIPT],
                "model config injection",
            )
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _configure(self, container: str, request: RunRequest, environment: Mapping[str, str]) -> str:
        version = self._checked(["exec", container, "openclaw", "--version"], "OpenClaw version").stdout.strip()
        self._inject_models(container)
        self._checked(
            ["exec", container, "openclaw", "models", "set", request.model],
            "model setup",
        )
        self._checked(
            ["exec", container, "openclaw", "config", "set", "gateway.port", str(self.gateway_port)],
            "gateway port setup",
        )
        thinking = request.task.thinking or self.config.thinking
        if thinking:
            self._checked(
                [
                    "exec",
                    container,
                    "openclaw",
                    "config",
                    "set",
                    "agents.defaults.thinkingDefault",
                    thinking,
                ],
                "thinking mode setup",
            )
        if self.config.search_provider:
            self._checked(
                [
                    "exec",
                    container,
                    "openclaw",
                    "config",
                    "set",
                    "tools.web.search.provider",
                    self.config.search_provider,
                ],
                "search provider setup",
            )
        if self.config.set_image_model:
            image_result = self.runtime.run(
                [
                    "exec",
                    container,
                    "openclaw",
                    "config",
                    "set",
                    "agents.defaults.imageModel.primary",
                    request.model,
                ]
            )
            if image_result.returncode != 0:
                logger.warning("[%s] image model setup was not supported", request.task.id)
        if environment.get("OPENROUTER_API_KEY"):
            self._checked(
                [
                    "exec",
                    "-e",
                    f"ROLLER_OPENROUTER_KEY={environment['OPENROUTER_API_KEY']}",
                    container,
                    "python3",
                    "-c",
                    self._AUTH_INJECT_SCRIPT,
                ],
                "OpenRouter auth setup",
            )
        return version

    def _prepare_context_and_setup(self, container: str, request: RunRequest) -> None:
        if request.bundle is not None:
            self._checked(
                [
                    "exec",
                    container,
                    "/bin/bash",
                    "-c",
                    f"cp -a {CONTEXT_CONTAINER_PATH}/. /root/ && "
                    f"cp -a {CONTEXT_CONTAINER_PATH}/. "
                    f"{self.config.workspace_container_path}/",
                ],
                "context injection",
            )
        else:
            persona = request.task.raw.get("persona")
            if isinstance(persona, str) and persona.strip():
                self._checked(
                    [
                        "exec",
                        "-e",
                        f"ROLLER_WORKSPACE={self.config.workspace_container_path}",
                        "-e",
                        f"ROLLER_PERSONA={persona.strip()}",
                        container,
                        "python3",
                        "-c",
                        self._PERSONA_SCRIPT,
                    ],
                    "persona setup",
                )
        for command in request.task.setup_commands:
            self._checked(
                [
                    "exec",
                    "-w",
                    self.config.workspace_container_path,
                    container,
                    "/bin/bash",
                    "-lc",
                    command,
                ],
                f"task setup command {command!r}",
            )

    def _wait_for_gateway(self, container: str, process: subprocess.Popen[str]) -> None:
        deadline = time.monotonic() + self.config.gateway_startup_timeout
        probe = (
            "import socket,sys; s=socket.socket(); s.settimeout(.2); "
            f"sys.exit(s.connect_ex(('127.0.0.1',{self.gateway_port})))"
        )
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"OpenClaw gateway exited early with code {process.returncode}")
            result = self.runtime.run(["exec", container, "python3", "-c", probe], timeout=2)
            if result.returncode == 0:
                return
            time.sleep(0.25)
        raise TimeoutError(
            f"OpenClaw gateway did not listen on port {self.gateway_port} within "
            f"{self.config.gateway_startup_timeout:g}s"
        )

    def _prompts(self, request: RunRequest) -> tuple[str, ...]:
        return request.task.prompts

    def _run_agent_turns(
        self,
        container: str,
        request: RunRequest,
        output_dir: Path,
        attempt: int,
        started: float,
    ) -> tuple[str, str | None, list[int]]:
        log_path = output_dir / "rollout" / f"agent.attempt-{attempt}.log"
        return_codes: list[int] = []
        for turn_index, prompt in enumerate(self._prompts(request)):
            remaining = request.timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                return "timeout", "task timeout elapsed before next turn", return_codes
            process = self.runtime.popen(
                [
                    "exec",
                    "-w",
                    self.config.workspace_container_path,
                    container,
                    "openclaw",
                    "agent",
                    "--session-id",
                    self.config.session_id,
                    "--timeout",
                    str(max(1, int(remaining))),
                    "--message",
                    prompt,
                ],
                log_path,
                append=turn_index > 0,
            )
            try:
                return_code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                self.runtime.run(["exec", container, "pkill", "-f", "openclaw agent"])
                return "timeout", f"task exceeded {request.timeout_seconds}s", return_codes
            finally:
                self.runtime.close_log(process)
            return_codes.append(return_code)
            if return_code != 0:
                return "error", f"OpenClaw agent turn {turn_index + 1} exited with code {return_code}", return_codes
        return "ok", None, return_codes

    def _collect_trace(self, container: str, output_dir: Path, attempt: int) -> Path | None:
        rollout_dir = output_dir / "rollout"
        rollout_dir.mkdir(parents=True, exist_ok=True)
        attempt_trace_path = rollout_dir / f"trace.attempt-{attempt}.jsonl"
        trace_path = rollout_dir / "trace.jsonl"
        container_path = (
            f"/root/.openclaw/agents/main/sessions/{self.config.session_id}.jsonl"
        )
        result = self.runtime.run(
            ["cp", f"{container}:{container_path}", str(attempt_trace_path)]
        )
        if result.returncode != 0:
            latest = self.runtime.run(
                ["exec", container, "python3", "-c", self._LATEST_SESSION_SCRIPT]
            )
            fallback_path = latest.stdout.strip() if latest.returncode == 0 else ""
            if fallback_path:
                result = self.runtime.run(
                    ["cp", f"{container}:{fallback_path}", str(attempt_trace_path)]
                )
        if result.returncode != 0 or not attempt_trace_path.is_file():
            return None
        shutil.copyfile(attempt_trace_path, trace_path)
        return trace_path

    def _collect_legacy_artifacts(self, container: str, output_dir: Path) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        artifact_root = output_dir / "artifacts" / "legacy"
        for index, container_path in enumerate(self.config.collect_paths):
            destination = artifact_root / f"{index:02d}_{safe_component(Path(container_path).name or 'root')}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            result = self.runtime.run(["cp", f"{container}:{container_path}", str(destination)])
            records.append(
                {
                    "container_path": container_path,
                    "output_path": str(destination) if result.returncode == 0 else None,
                    "error": None if result.returncode == 0 else self._error_text(result),
                }
            )
        return records

    def _collect_contract_outputs(
        self, container: str, request: RunRequest, output_dir: Path
    ) -> tuple[ArtifactCollectionResult, str | None]:
        staging_root = output_dir / ".runtime-outputs"
        if staging_root.exists():
            shutil.rmtree(staging_root)
        staging_root.mkdir(parents=True, exist_ok=True)
        result = self.runtime.run(
            [
                "cp",
                f"{container}:{self.config.workspace_container_path}/outputs/.",
                str(staging_root),
            ]
        )
        copy_error = None if result.returncode == 0 else self._error_text(result)
        try:
            collected = collect_output_artifacts(
                staging_root, output_dir, request.task.outputs
            )
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
        return collected, copy_error

    def _image_digest(self) -> str | None:
        result = self.runtime.run(
            ["image", "inspect", self.config.docker_image, "--format", "{{.Id}}"]
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    @staticmethod
    def _stop_process(process: subprocess.Popen[str] | None) -> None:
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def run(self, request: RunRequest, output_dir: Path, attempt: int) -> HarnessResult:
        started = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # The canonical trace always represents the latest attempt; numbered raw
        # traces remain available for retry diagnosis.
        (output_dir / "rollout" / "trace.jsonl").unlink(missing_ok=True)
        container = (
            f"roller-{request.index}-{safe_component(request.model, 24)}-"
            f"{uuid.uuid4().hex[:8]}"
        ).lower()
        gateway_process: subprocess.Popen[str] | None = None
        container_started = False
        status = "error"
        error: str | None = None
        version = ""
        return_codes: list[int] = []
        trace_path: Path | None = None
        legacy_artifact_records: list[dict[str, Any]] = []
        artifact_result: ArtifactCollectionResult | None = None
        output_copy_error: str | None = None
        environment: dict[str, str] = {}
        image_digest = self._image_digest()

        try:
            environment = self._start_container(container, request)
            container_started = True
            self._prepare_workspace(container, request)
            self._prepare_context_and_setup(container, request)
            version = self._configure(container, request, environment)
            gateway_process = self.runtime.popen(
                [
                    "exec",
                    "-w",
                    self.config.workspace_container_path,
                    container,
                    "openclaw",
                    "gateway",
                    "--port",
                    str(self.gateway_port),
                ],
                output_dir / "rollout" / f"gateway.attempt-{attempt}.log",
            )
            self._wait_for_gateway(container, gateway_process)
            status, error, return_codes = self._run_agent_turns(
                container, request, output_dir, attempt, started
            )
        except subprocess.TimeoutExpired:
            status = "timeout"
            error = f"Docker operation timed out after {request.timeout_seconds}s"
        except Exception as exc:
            status = "error"
            error = str(exc)
        finally:
            if container_started:
                try:
                    trace_path = self._collect_trace(container, output_dir, attempt)
                except Exception as exc:
                    logger.warning("[%s] trace collection failed: %s", request.task.id, exc)
                try:
                    artifact_result, output_copy_error = self._collect_contract_outputs(
                        container, request, output_dir
                    )
                except Exception as exc:
                    output_copy_error = str(exc)
                    logger.warning("[%s] output collection failed: %s", request.task.id, exc)
                try:
                    legacy_artifact_records = self._collect_legacy_artifacts(
                        container, output_dir
                    )
                except Exception as exc:
                    logger.warning("[%s] legacy artifact collection failed: %s", request.task.id, exc)
            try:
                self._stop_process(gateway_process)
            finally:
                self.runtime.close_log(gateway_process)

            if status == "ok" and trace_path is None:
                status = "error"
                error = "OpenClaw finished but no session trace was found"
            if status == "ok" and output_copy_error is not None:
                status = "error"
                error = f"output collection failed: {output_copy_error}"
            if status == "ok" and artifact_result is not None and not artifact_result.passed:
                status = "error"
                error = "output contract validation failed"
            if container_started and not (self.config.keep_container_on_error and status != "ok"):
                try:
                    remove_result = self.runtime.run(["rm", "-f", container], timeout=30)
                    if remove_result.returncode != 0:
                        logger.warning("[%s] failed to remove container %s", request.task.id, container)
                except Exception as exc:
                    logger.warning(
                        "[%s] failed to remove container %s: %s",
                        request.task.id,
                        container,
                        exc,
                    )

        elapsed = time.monotonic() - started
        usage: dict[str, Any] = (
            extract_openclaw_usage(trace_path) if trace_path is not None else dict(ZERO_USAGE)
        )
        usage["elapsed_seconds"] = round(elapsed, 3)
        rollout_dir = output_dir / "rollout"
        rollout_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(rollout_dir / "usage.json", usage)
        api_base_urls = _model_base_urls(self.config.model_config or {})
        if environment.get("OPENROUTER_BASE_URL"):
            api_base_urls.append(_redact_url(environment["OPENROUTER_BASE_URL"]))
        provenance = {
            "schema_version": "roller.provenance/v1",
            "roller_version": __version__,
            "task_id": request.task.id,
            "task_bundle_digest": request.bundle.manifest_digest if request.bundle else None,
            "input_tree_digest": request.bundle.input_tree_digest if request.bundle else None,
            "input_manifest_digest": (
                request.bundle.input_manifest_digest if request.bundle else None
            ),
            "context_manifest_digest": (
                request.bundle.context_manifest_digest if request.bundle else None
            ),
            "resume_safe": request.bundle.resume_safe if request.bundle else None,
            "harness": self.name,
            "openclaw_version": version,
            "docker_image": self.config.docker_image,
            "docker_image_digest": image_digest,
            "model": request.model,
            "api_base_urls": list(dict.fromkeys(api_base_urls)),
            "environment_names": sorted(environment),
            "attempt": attempt,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        atomic_write_json(output_dir / "provenance.json", provenance)
        output_contract_metadata = (
            {
                "passed": artifact_result.passed,
                "manifest": artifact_result.manifest_path.relative_to(output_dir).as_posix(),
                "validation": artifact_result.validation_path.relative_to(output_dir).as_posix(),
                "declared_files": artifact_result.declared_files,
                "undeclared_files": artifact_result.undeclared_files,
                "errors": list(artifact_result.errors),
            }
            if artifact_result is not None
            else None
        )
        return HarnessResult(
            status=status,
            elapsed_seconds=elapsed,
            error=error,
            usage=usage,
            trace_path=(
                trace_path.relative_to(output_dir).as_posix()
                if trace_path is not None
                else None
            ),
            metadata={
                "openclaw_version": version,
                "gateway_port": self.gateway_port,
                "container": container if self.config.keep_container_on_error and status != "ok" else None,
                "agent_return_codes": return_codes,
                "turns": len(self._prompts(request)),
                "output_contract": output_contract_metadata,
                "output_copy_error": output_copy_error,
                "legacy_artifacts": legacy_artifact_records,
                "attempt": attempt,
            },
        )
