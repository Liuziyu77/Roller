from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from roller.artifacts import ArtifactCollectionResult, collect_output_directory
from roller.codex_actf import convert_codex_trace
from roller.harnesses.base import Harness, InteractiveHarness, InteractiveSession, TurnResult
from roller.harnesses.docker import DockerRuntime, remove_container
from roller.models import HarnessResult, RunRequest
from roller.native_images import attachment_specs
from roller.storage import safe_component
from roller.thinking import normalize_thinking, resolve_thinking
from roller.usage import ZERO_USAGE, extract_codex_usage
from roller.version import __version__
from roller.wire_trace import WireTraceProxy, extract_wire_tools, redact_jsonl_file

logger = logging.getLogger(__name__)

DEFAULT_CODEX_IMAGE = "roller-codex:0.149.0"
CODEX_HOME_CONTAINER_PATH = "/roller/codex-home"
INPUTS_CONTAINER_PATH = "/roller/task/inputs"
CONTEXT_CONTAINER_PATH = "/roller/task/context"
OUTPUTS_CONTAINER_PATH = "/roller/task/outputs"
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

COMMON_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


def redact_api_base(value: str) -> str:
    """Return an API endpoint safe to retain in run metadata."""

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return "<invalid-url>"
    netloc = hostname
    if port is not None:
        netloc = f"{hostname}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def validate_codex_thinking(version: str, effort: str | None) -> None:
    """Reject effort levels unsupported by the pinned default Codex CLI."""

    if effort in {"max", "ultra"} and re.search(
        r"(?:^|\s)0\.149\.0(?:\s|$)", version
    ):
        raise RuntimeError(
            f"Codex {version!r} does not accept reasoning effort {effort!r}; "
            "use minimal|low|medium|high|xhigh (xhigh is model-dependent), "
            "or select a later image that explicitly supports the requested level"
        )


@dataclass(frozen=True)
class CodexConfig:
    docker_image: str = DEFAULT_CODEX_IMAGE
    container_user: str | None = "0"
    network: str | None = "host"
    workspace_container_path: str = "/roller/workspace"
    config_path: Path | None = None
    api_base: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    pass_env: tuple[str, ...] = COMMON_ENV_NAMES
    reasoning_effort: str | None = None
    reasoning_summary: str | None = None
    web_search: str = "disabled"
    supports_websockets: bool = False
    wire_trace_mode: str = "actf"
    keep_thinking: bool = True
    keep_container_on_error: bool = False
    docker_binary: str = "docker"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reasoning_effort",
            normalize_thinking(
                self.reasoning_effort, field="Codex reasoning effort"
            ),
        )
        if not self.docker_image.strip():
            raise ValueError("Codex docker image must be non-empty")
        if self.container_user is not None and not self.container_user.strip():
            raise ValueError("Codex container user must be non-empty or None")
        if not self.workspace_container_path.startswith("/"):
            raise ValueError("Codex workspace container path must be absolute")
        if not ENV_NAME_RE.fullmatch(self.api_key_env):
            raise ValueError(f"invalid Codex API key environment name: {self.api_key_env!r}")
        if self.api_base:
            try:
                parsed = urlsplit(self.api_base)
                parsed.port
            except ValueError as exc:
                raise ValueError(
                    "Codex API base must be an absolute http(s) URL"
                ) from exc
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Codex API base must be an absolute http(s) URL")
        if self.reasoning_summary not in {None, "auto", "concise", "detailed", "none"}:
            raise ValueError("Codex reasoning summary must be auto|concise|detailed|none")
        if self.web_search not in {"disabled", "cached", "live"}:
            raise ValueError("Codex web search must be disabled|cached|live")
        if self.wire_trace_mode not in {"off", "actf", "full"}:
            raise ValueError("Codex wire trace mode must be off|actf|full")
        if self.wire_trace_mode == "full" and not self.api_base:
            raise ValueError("Codex --wire-trace full requires --api-base")
        if self.wire_trace_mode == "full" and self.network != "host":
            raise ValueError(
                "Codex --wire-trace full requires "
                "--network host so the container can reach the rollout-local proxy"
            )
        if self.config_path is not None and not self.config_path.is_file():
            raise FileNotFoundError(f"Codex config not found: {self.config_path}")


class CodexHarness(InteractiveHarness):
    """Run Codex CLI in an isolated container and retain its native session JSONL."""

    name = "codex"

    def __init__(
        self,
        config: CodexConfig,
        worker_slot: int,
        runtime: DockerRuntime | None = None,
    ) -> None:
        self.config = config
        self.worker_slot = worker_slot
        self.runtime = runtime or DockerRuntime(config.docker_binary)
        self._wire_proxy: WireTraceProxy | None = None

    def create_session(
        self,
        request: RunRequest,
        output_dir: Path,
        attempt: int,
        *,
        collect_outputs: bool = True,
    ) -> InteractiveSession:
        return CodexSession(self, request, output_dir, attempt, collect_outputs=collect_outputs)

    def _start_wire_proxy(self, output_dir: Path) -> None:
        self._wire_proxy = None
        if self.config.wire_trace_mode != "full":
            return
        if not self.config.api_base:
            raise RuntimeError("Codex wire proxy requires --api-base")
        log_path = output_dir / "wire_trace.jsonl"
        self._wire_proxy = WireTraceProxy(
            self.config.api_base,
            log_path,
            mode="full",
        )
        self._wire_proxy.start()

    def _stop_wire_proxy(self) -> None:
        if self._wire_proxy is None:
            return
        proxy = self._wire_proxy
        try:
            proxy.stop()
            if proxy.log_path.is_file():
                redact_jsonl_file(proxy.log_path)
        finally:
            self._wire_proxy = None

    @staticmethod
    def _error_text(result: subprocess.CompletedProcess[str]) -> str:
        return (result.stderr or result.stdout or "unknown error").strip()[-2000:]

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
                raise ValueError(
                    f"task {request.task.id!r} requires missing environment variable {name}"
                )
            environment[name] = os.environ[name]

        api_key = os.environ.get(self.config.api_key_env)
        if not api_key:
            raise ValueError(
                f"Codex requires a non-empty API key in {self.config.api_key_env}"
            )
        environment["OPENAI_API_KEY"] = api_key
        if self.config.api_base:
            environment["OPENAI_BASE_URL"] = self.config.api_base
        return environment

    def _start_container(
        self, container: str, request: RunRequest
    ) -> dict[str, str]:
        environment = self._environment(request)
        args = [
            "run",
            "-d",
            "--name",
            container,
            "--label",
            "ai.roller.harness=codex",
        ]
        if self.config.container_user is not None:
            args.extend(["--user", self.config.container_user])
        if self.config.network:
            args.extend(["--network", self.config.network])
        for key, value in sorted(environment.items()):
            args.extend(["-e", f"{key}={value}"])
        args.extend(["-e", f"CODEX_HOME={CODEX_HOME_CONTAINER_PATH}"])

        if request.bundle is not None:
            args.extend(["-v", f"{request.bundle.inputs_root}:{INPUTS_CONTAINER_PATH}:ro"])
            args.extend(["-v", f"{request.bundle.context_root}:{CONTEXT_CONTAINER_PATH}:ro"])
            if (
                not request.bundle.legacy_workspace
                and request.task.input_mode == "mount"
            ):
                for input_file in request.task.inputs:
                    input_path = request.bundle.inputs_root / input_file.path
                    if input_path.exists():
                        args.extend(
                            [
                                "-v",
                                f"{input_path}:"
                                f"{self.config.workspace_container_path}/{input_file.path}:ro",
                            ]
                        )
        elif request.task.workspace is not None:
            if not request.task.workspace.is_dir():
                raise FileNotFoundError(
                    f"task workspace is not a directory: {request.task.workspace}"
                )
            args.extend(["-v", f"{request.task.workspace}:/roller-input:ro"])

        args.extend(
            [self.config.docker_image, "/bin/bash", "-c", "tail -f /dev/null"]
        )
        self._checked(args, "Codex container startup", timeout=60)
        return environment

    def _prepare_workspace(self, container: str, request: RunRequest) -> None:
        workspace = self.config.workspace_container_path
        output_root = f"{workspace}/{request.task.output_dir}"
        if request.bundle is not None:
            if request.bundle.legacy_workspace or request.task.input_mode == "copy":
                setup = (
                    f"mkdir -p {shlex.quote(workspace)} && "
                    f"cp -a {INPUTS_CONTAINER_PATH}/. {shlex.quote(workspace)}/ && "
                    f"chmod -R u+w {shlex.quote(workspace)} && "
                    f"rm -rf {shlex.quote(workspace)}/context && "
                    f"ln -s {CONTEXT_CONTAINER_PATH} {shlex.quote(workspace)}/context"
                )
            else:
                setup = f"mkdir -p {shlex.quote(workspace)}"
        elif request.task.workspace is not None:
            setup = (
                f"mkdir -p {shlex.quote(workspace)} && "
                f"cp -a /roller-input/. {shlex.quote(workspace)}/ && "
                f"chmod -R u+w {shlex.quote(workspace)}"
            )
        else:
            setup = f"mkdir -p {shlex.quote(workspace)}"
        script = (
            f"{setup} && rm -rf {shlex.quote(output_root)} && "
            f"mkdir -p {shlex.quote(output_root)} {CODEX_HOME_CONTAINER_PATH} && "
            f"mkdir -p /roller/task && rm -rf {OUTPUTS_CONTAINER_PATH} && "
            f"ln -s {shlex.quote(output_root)} {OUTPUTS_CONTAINER_PATH}"
        )
        self._checked(
            ["exec", container, "/bin/bash", "-c", script],
            "Codex workspace setup",
        )

    def _prepare_context_and_setup(
        self, container: str, request: RunRequest
    ) -> None:
        if request.bundle is not None:
            workspace = shlex.quote(self.config.workspace_container_path)
            script = (
                f"cp -a {CONTEXT_CONTAINER_PATH}/. {workspace}/ && "
                "if [ -d /roller/task/context/skills ]; then "
                "mkdir -p \"$HOME/.agents/skills\" && "
                "cp -a /roller/task/context/skills/. \"$HOME/.agents/skills/\"; fi"
            )
            self._checked(
                ["exec", container, "/bin/bash", "-c", script],
                "Codex context injection",
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

    def _prepare_config(self, container: str) -> None:
        self._checked(
            ["exec", container, "mkdir", "-p", CODEX_HOME_CONTAINER_PATH],
            "Codex home setup",
        )
        if self.config.config_path is None:
            return
        self._checked(
            [
                "cp",
                str(self.config.config_path),
                f"{container}:{CODEX_HOME_CONTAINER_PATH}/config.toml",
            ],
            "Codex config copy",
        )


    def _effective_effort(self, request: RunRequest) -> str | None:
        effort = resolve_thinking(request.task.thinking, self.config.reasoning_effort)
        if effort == "off":
            raise ValueError(
                "Codex does not expose a harness-level reasoning-off mode; omit "
                "--thinking to use the model default or choose an explicit effort"
            )
        if effort == "adaptive":
            raise ValueError(
                "Codex does not accept the OpenClaw-specific adaptive thinking profile"
            )
        return effort

    def _codex_command(
        self,
        container: str,
        request: RunRequest,
        prompt: str,
        *,
        resume: bool,
    ) -> list[str]:
        command = [
            "exec",
            "-e",
            f"CODEX_HOME={CODEX_HOME_CONTAINER_PATH}",
            "-w",
            self.config.workspace_container_path,
            container,
            "codex",
            "exec",
        ]
        if resume:
            command.extend(["resume", "--last"])
        command.extend(
            [
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
                "--model",
                request.model,
                "--json",
                "--enable",
                "unified_exec",
            ]
        )
        effort = self._effective_effort(request)
        if effort:
            command.extend(["-c", f"model_reasoning_effort={json.dumps(effort)}"])
        if self.config.reasoning_summary:
            command.extend(
                [
                    "-c",
                    "model_reasoning_summary="
                    f"{json.dumps(self.config.reasoning_summary)}",
                ]
            )
        command.extend(["-c", f"web_search={json.dumps(self.config.web_search)}"])
        api_base = (
            self._wire_proxy.proxy_base_url
            if self._wire_proxy is not None
            else self.config.api_base
        )
        if api_base:
            command.extend(
                [
                    "-c",
                    'model_provider="roller_api"',
                    "-c",
                    'model_providers.roller_api.name="Roller API"',
                    "-c",
                    "model_providers.roller_api.base_url="
                    f"{json.dumps(api_base)}",
                    "-c",
                    'model_providers.roller_api.env_key="OPENAI_API_KEY"',
                    "-c",
                    'model_providers.roller_api.wire_api="responses"',
                    "-c",
                    "model_providers.roller_api.supports_websockets="
                    f"{str(self.config.supports_websockets).lower()}",
                    "-c",
                    "disable_response_storage=true",
                ]
            )
        for image in attachment_specs(request, self.config.workspace_container_path, first_turn=not resume):
            command.extend(["--image", image["path"]])
        command.extend(["--", prompt])
        return command

    def _run_turn(
        self,
        container: str,
        request: RunRequest,
        prompt: str,
        *,
        resume: bool,
        timeout_seconds: float,
        log_path: Path,
    ) -> int:
        process = self.runtime.popen(
            self._codex_command(container, request, prompt, resume=resume),
            log_path,
            append=resume,
        )
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            self.runtime.run(["exec", container, "pkill", "-f", "codex exec"])
            raise TimeoutError("Codex turn timed out") from exc
        finally:
            self.runtime.close_log(process)
        if return_code != 0:
            detail = ""
            try:
                detail = log_path.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()[-2000:]
            except OSError:
                pass
            raise RuntimeError(
                f"Codex exec exited with code {return_code}"
                + (f": {detail}" if detail else "")
            )
        return return_code

    def _collect_trace(
        self, container: str, output_dir: Path, attempt: int
    ) -> Path | None:
        rollout_dir = output_dir / "rollout"
        rollout_dir.mkdir(parents=True, exist_ok=True)
        destination = rollout_dir / f"trace.attempt-{attempt}.jsonl"
        destination.unlink(missing_ok=True)
        finder = (
            "from pathlib import Path; "
            f"p=Path({CODEX_HOME_CONTAINER_PATH!r})/'sessions'; "
            "rows=[x for x in p.rglob('*.jsonl') if x.is_file()]; "
            "print(max(rows,key=lambda x:x.stat().st_mtime_ns) if rows else '')"
        )
        found = self.runtime.run(["exec", container, "python3", "-c", finder])
        source = found.stdout.strip() if found.returncode == 0 else ""
        if not source:
            return None
        copied = self.runtime.run(["cp", f"{container}:{source}", str(destination)])
        if copied.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
            return None
        trace_path = rollout_dir / "trace.jsonl"
        shutil.copyfile(destination, trace_path)
        return trace_path

    def _collect_outputs(
        self, container: str, request: RunRequest, output_dir: Path
    ) -> tuple[ArtifactCollectionResult, str | None]:
        staging_root = output_dir / ".runtime-outputs"
        if staging_root.exists():
            shutil.rmtree(staging_root)
        staging_root.mkdir(parents=True, exist_ok=True)
        result = self.runtime.run(
            [
                "cp",
                f"{container}:{self.config.workspace_container_path}/"
                f"{request.task.output_dir}/.",
                str(staging_root),
            ]
        )
        copy_error = None if result.returncode == 0 else self._error_text(result)
        try:
            collected = collect_output_directory(staging_root, output_dir)
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
        return collected, copy_error

    def _image_digest(self) -> str | None:
        result = self.runtime.run(
            ["image", "inspect", self.config.docker_image, "--format", "{{.Id}}"]
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def run(self, request: RunRequest, output_dir: Path, attempt: int) -> HarnessResult:
        started = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        container = (
            f"roller-codex-{request.index}-{safe_component(request.display_model, 20)}-"
            f"{uuid.uuid4().hex[:8]}"
        ).lower()
        container_started = False
        status = "error"
        error: str | None = None
        return_codes: list[int] = []
        trace_path: Path | None = None
        trajectory_path: Path | None = None
        artifact_result: ArtifactCollectionResult | None = None
        output_copy_error: str | None = None
        codex_version = ""
        environment: dict[str, str] = {}
        image_digest = self._image_digest()
        log_path = output_dir / "rollout" / f"codex.attempt-{attempt}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self._start_wire_proxy(output_dir)
            environment = self._start_container(container, request)
            container_started = True
            self._prepare_workspace(container, request)
            self._prepare_context_and_setup(container, request)
            self._prepare_config(container)
            codex_version = self._checked(
                ["exec", container, "codex", "--version"],
                "Codex version check",
            ).stdout.strip()
            validate_codex_thinking(
                codex_version, self._effective_effort(request)
            )
            for index, prompt in enumerate(request.task.prompts):
                remaining = request.timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("Codex task timed out")
                return_codes.append(
                    self._run_turn(
                        container,
                        request,
                        prompt,
                        resume=index > 0,
                        timeout_seconds=remaining,
                        log_path=log_path,
                    )
                )
            status = "ok"
        except (TimeoutError, subprocess.TimeoutExpired):
            status = "timeout"
            error = f"task exceeded {request.timeout_seconds}s"
        except Exception as exc:
            status = "error"
            error = str(exc)
        finally:
            try:
                self._stop_wire_proxy()
            except Exception as exc:
                logger.warning("[%s] Codex wire proxy shutdown failed: %s", request.task.id, exc)

        if container_started:
            try:
                trace_path = self._collect_trace(container, output_dir, attempt)
            except Exception as exc:
                logger.warning("[%s] Codex trace collection failed: %s", request.task.id, exc)
            try:
                artifact_result, output_copy_error = self._collect_outputs(
                    container, request, output_dir
                )
            except Exception as exc:
                output_copy_error = str(exc)
                logger.warning("[%s] Codex output collection failed: %s", request.task.id, exc)

        if trace_path is not None:
            try:
                trajectory_path = output_dir / "rollout" / "trace_ACTF.json"
                trajectory = convert_codex_trace(
                    trace_path,
                    trajectory_path,
                    user_messages=request.task.prompts,
                    keep_thinking=self.config.keep_thinking,
                    tools=extract_wire_tools(output_dir / "wire_trace.jsonl"),
                )
                if not trajectory.get("steps"):
                    raise ValueError("Codex native trace produced no assistant steps")
            except Exception as exc:
                trajectory_path = None
                if status == "ok":
                    status = "error"
                    error = f"Codex trajectory conversion failed: {exc}"

        if status == "ok" and trace_path is None:
            status = "error"
            error = "Codex finished but no native session trace was found"
        if status == "ok" and output_copy_error is not None:
            status = "error"
            error = f"output collection failed: {output_copy_error}"
        if status == "ok" and artifact_result is not None and not artifact_result.passed:
            status = "error"
            error = "unsafe output files were rejected"

        retained_container: str | None = None
        if container_started:
            if self.config.keep_container_on_error and status != "ok":
                retained_container = container
            else:
                removed = remove_container(self.runtime, container)
                if removed.returncode != 0:
                    logger.warning("[%s] failed to remove container %s", request.task.id, container)

        elapsed = time.monotonic() - started
        usage: dict[str, Any] = (
            extract_codex_usage(trace_path) if trace_path is not None else dict(ZERO_USAGE)
        )
        usage["elapsed_seconds"] = round(elapsed, 3)
        output_metadata = (
            {
                "mode": "all",
                "agent_output_dir": request.task.output_dir,
                "passed": artifact_result.passed,
                "path": (
                    artifact_result.results_path.relative_to(output_dir).as_posix()
                    if artifact_result.results_path is not None
                    else None
                ),
                "file_count": artifact_result.file_count,
                "directory_count": artifact_result.directory_count,
                "files": list(artifact_result.files),
                "errors": list(artifact_result.errors),
            }
            if artifact_result is not None
            else None
        )
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
            "codex_version": codex_version,
            "docker_image": self.config.docker_image,
            "docker_image_digest": image_digest,
            "model": request.model,
            "api_base_urls": (
                [redact_api_base(self.config.api_base)] if self.config.api_base else []
            ),
            "environment_names": sorted(environment),
            "attempt": attempt,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
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
            trajectory_path=(
                trajectory_path.relative_to(output_dir).as_posix()
                if trajectory_path is not None
                else None
            ),
            metadata={
                "raw_trace_schema": "codex-session-jsonl",
                "codex_version": codex_version,
                "container": retained_container,
                "agent_return_codes": return_codes,
                "turns": len(request.task.prompts),
                "reasoning_effort": resolve_thinking(
                    request.task.thinking, self.config.reasoning_effort
                ),
                "wire_trace": self.config.wire_trace_mode,
                "reasoning_summary": self.config.reasoning_summary,
                "keep_thinking": self.config.keep_thinking,
                "web_search": self.config.web_search,
                "supports_websockets": self.config.supports_websockets,
                "output_collection": output_metadata,
                "output_copy_error": output_copy_error,
                "attempt": attempt,
                "provenance": provenance,
            },
        )


class CodexSession(InteractiveSession):
    """Persistent Codex CLI session used by the conversation manager.

    Codex's ``resume --last`` keeps the native session alive in one container;
    this adapter exposes one turn at a time while retaining the same trace and
    output semantics as :meth:`CodexHarness.run`.
    """

    def __init__(
        self,
        harness: CodexHarness,
        request: RunRequest,
        output_dir: Path,
        attempt: int,
        *,
        collect_outputs: bool = True,
    ) -> None:
        self.harness = harness
        self.request = request
        self.output_dir = output_dir
        self.attempt = attempt
        self.collect_outputs = collect_outputs
        self.started = False
        self.container_started = False
        self.container = (
            f"roller-codex-{request.index}-{safe_component(request.display_model, 20)}-"
            f"{uuid.uuid4().hex[:8]}"
        ).lower()
        self.messages: list[str] = []
        self.return_codes: list[int] = []
        self._pending_message = ""
        self.environment: dict[str, str] = {}
        self.codex_version = ""
        self.image_digest = harness._image_digest()
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.started_monotonic = time.monotonic()
        self.log_path = output_dir / "rollout" / f"codex.attempt-{attempt}.jsonl"
        self._finished: HarnessResult | None = None

    def start(self) -> None:
        if self.started:
            raise RuntimeError("Codex session has already started")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        for relative in (
            "rollout/trace.jsonl",
            f"rollout/trace.attempt-{self.attempt}.jsonl",
            "wire_trace.jsonl",
        ):
            (self.output_dir / relative).unlink(missing_ok=True)
        try:
            self.harness._start_wire_proxy(self.output_dir)
            self.environment = self.harness._start_container(self.container, self.request)
            self.container_started = True
            self.harness._prepare_workspace(self.container, self.request)
            self.harness._prepare_context_and_setup(self.container, self.request)
            self.harness._prepare_config(self.container)
            self.codex_version = self.harness._checked(
                ["exec", self.container, "codex", "--version"],
                "Codex version check",
            ).stdout.strip()
            validate_codex_thinking(
                self.codex_version, self.harness._effective_effort(self.request)
            )
            self.started = True
        except Exception:
            # ``finish`` is still responsible for stopping the proxy/container.
            raise

    def _latest_reply(self) -> str:
        trace = self.harness._collect_trace(self.container, self.output_dir, self.attempt)
        if trace is None:
            raise RuntimeError("Codex turn produced no native session trace")
        destination = self.output_dir / "rollout" / f".turn-{len(self.messages) + 1}.ACTF.json"
        trajectory = convert_codex_trace(
            trace,
            destination,
            user_messages=[*self.messages, self._pending_message],
            keep_thinking=self.harness.config.keep_thinking,
            tools=extract_wire_tools(self.output_dir / "wire_trace.jsonl"),
        )
        steps = trajectory.get("steps")
        if not isinstance(steps, list):
            raise RuntimeError("Codex turn produced an invalid ACTF trajectory")
        for step in reversed(steps):
            if not isinstance(step, Mapping):
                continue
            assistant = step.get("assistant_content")
            if not isinstance(assistant, Mapping):
                continue
            content = assistant.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
        raise RuntimeError("Codex turn produced no visible assistant reply")

    def send(self, message: str, timeout_seconds: float) -> TurnResult:
        if not self.started or not self.container_started:
            raise RuntimeError("Codex session has not started")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("Codex session messages must be non-empty strings")
        prompt = message.strip()
        turn_started = time.monotonic()
        self._pending_message = prompt
        self.return_codes.append(
            self.harness._run_turn(
                self.container,
                self.request,
                prompt,
                resume=bool(self.messages),
                timeout_seconds=timeout_seconds,
                log_path=self.log_path,
            )
        )
        reply = self._latest_reply()
        self.messages.append(prompt)
        return TurnResult(reply, time.monotonic() - turn_started, self.return_codes[-1])

    def finish(self, status: str, error: str | None = None) -> HarnessResult:
        if self._finished is not None:
            return self._finished
        config = self.harness.config
        trace_path: Path | None = None
        trajectory_path: Path | None = None
        artifact_result: ArtifactCollectionResult | None = None
        output_copy_error: str | None = None
        final_status = status
        final_error = error
        try:
            self.harness._stop_wire_proxy()
        except Exception as exc:
            logger.warning("[%s] Codex wire proxy shutdown failed: %s", self.request.task.id, exc)
        if self.container_started:
            try:
                trace_path = self.harness._collect_trace(self.container, self.output_dir, self.attempt)
            except Exception as exc:
                logger.warning("[%s] Codex trace collection failed: %s", self.request.task.id, exc)
            if self.collect_outputs:
                try:
                    artifact_result, output_copy_error = self.harness._collect_outputs(
                        self.container, self.request, self.output_dir
                    )
                except Exception as exc:
                    output_copy_error = str(exc)
                    logger.warning("[%s] Codex output collection failed: %s", self.request.task.id, exc)
        if trace_path is not None:
            try:
                trajectory_path = self.output_dir / "rollout" / "trace_ACTF.json"
                trajectory = convert_codex_trace(
                    trace_path,
                    trajectory_path,
                    user_messages=self.messages,
                    keep_thinking=config.keep_thinking,
                    tools=extract_wire_tools(self.output_dir / "wire_trace.jsonl"),
                )
                if not trajectory.get("steps"):
                    raise ValueError("Codex native trace produced no assistant steps")
            except Exception as exc:
                trajectory_path = None
                if final_status == "ok":
                    final_status, final_error = "error", f"Codex trajectory conversion failed: {exc}"
        if final_status == "ok" and trace_path is None:
            final_status, final_error = "error", "Codex finished but no native session trace was found"
        if final_status == "ok" and output_copy_error is not None:
            final_status, final_error = "error", f"output collection failed: {output_copy_error}"
        if final_status == "ok" and artifact_result is not None and not artifact_result.passed:
            final_status, final_error = "error", "unsafe output files were rejected"
        retained_container: str | None = None
        if self.container_started:
            if config.keep_container_on_error and final_status != "ok":
                retained_container = self.container
            else:
                removed = remove_container(self.harness.runtime, self.container)
                if removed.returncode != 0:
                    logger.warning("[%s] failed to remove container %s", self.request.task.id, self.container)
        elapsed = time.monotonic() - self.started_monotonic
        usage = extract_codex_usage(trace_path) if trace_path is not None else dict(ZERO_USAGE)
        usage["elapsed_seconds"] = round(elapsed, 3)
        output_metadata = None
        if artifact_result is not None:
            output_metadata = {
                "mode": "all",
                "agent_output_dir": self.request.task.output_dir,
                "passed": artifact_result.passed,
                "path": (
                    artifact_result.results_path.relative_to(self.output_dir).as_posix()
                    if artifact_result.results_path is not None else None
                ),
                "file_count": artifact_result.file_count,
                "directory_count": artifact_result.directory_count,
                "files": list(artifact_result.files),
                "errors": list(artifact_result.errors),
            }
        provenance = {
            "schema_version": "roller.provenance/v1",
            "roller_version": __version__,
            "task_id": self.request.task.id,
            "task_bundle_digest": self.request.bundle.manifest_digest if self.request.bundle else None,
            "input_tree_digest": self.request.bundle.input_tree_digest if self.request.bundle else None,
            "input_manifest_digest": self.request.bundle.input_manifest_digest if self.request.bundle else None,
            "context_manifest_digest": self.request.bundle.context_manifest_digest if self.request.bundle else None,
            "resume_safe": self.request.bundle.resume_safe if self.request.bundle else None,
            "harness": self.harness.name,
            "codex_version": self.codex_version,
            "docker_image": config.docker_image,
            "docker_image_digest": self.image_digest,
            "model": self.request.model,
            "api_base_urls": [redact_api_base(config.api_base)] if config.api_base else [],
            "environment_names": sorted(self.environment),
            "attempt": self.attempt,
            "started_at": self.started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        try:
            reasoning_effort = resolve_thinking(
                self.request.task.thinking, config.reasoning_effort
            )
        except Exception:
            reasoning_effort = None
        result = HarnessResult(
            status=final_status,
            elapsed_seconds=elapsed,
            error=final_error,
            usage=usage,
            trace_path=trace_path.relative_to(self.output_dir).as_posix() if trace_path else None,
            trajectory_path=trajectory_path.relative_to(self.output_dir).as_posix() if trajectory_path else None,
            metadata={
                "raw_trace_schema": "codex-session-jsonl",
                "codex_version": self.codex_version,
                "container": retained_container,
                "agent_return_codes": self.return_codes,
                "turns": len(self.messages),
                "reasoning_effort": reasoning_effort,
                "wire_trace": config.wire_trace_mode,
                "reasoning_summary": config.reasoning_summary,
                "keep_thinking": config.keep_thinking,
                "web_search": config.web_search,
                "supports_websockets": config.supports_websockets,
                "output_collection": output_metadata,
                "output_copy_error": output_copy_error,
                "attempt": self.attempt,
                "provenance": provenance,
            },
        )
        self._finished = result
        return result
