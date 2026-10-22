from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from roller.capture_runtime import CaptureRuntime
from roller.artifacts import ArtifactCollectionResult
from roller.codex_actf import convert_codex_trace
from roller.codex_capture import retain_root_tools
from roller.harnesses.base import InteractiveHarness, InteractiveSession, TurnResult
from roller.harnesses.container import (
    CONTEXT_CONTAINER_PATH,
    ENV_NAME as ENV_NAME_RE,
    INPUTS_CONTAINER_PATH,
    OUTPUTS_CONTAINER_PATH,
    PROXY_ENV_NAMES as COMMON_ENV_NAMES,
    ContainerCommands,
    build_provenance,
    docker_run_args,
    output_collection_metadata,
)
from roller.harnesses.docker import DockerRuntime, remove_container
from roller.models import HarnessResult, RunRequest
from roller.storage import safe_component
from roller.thinking import normalize_thinking, resolve_thinking
from roller.usage import ZERO_USAGE, extract_codex_usage
from roller.wire_trace import WireTraceProxy, redact_jsonl_file

logger = logging.getLogger(__name__)

DEFAULT_CODEX_IMAGE = "roller-codex:0.149.0"
CODEX_HOME_CONTAINER_PATH = "/roller/codex-home"
# Codex always replays reasoning.encrypted_content with store=false; give
# relayed endpoints a few chances before one rejected replay ends the rollout.
CODEX_ENCRYPTED_CONTENT_RETRIES = 3


def codex_effort(value: str | None) -> str | None:
    """Validate a logical effort using the same rules as Codex execution."""
    effort = normalize_thinking(value, field="Codex reasoning effort")
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
    capture_calls: bool = False
    capture_debug: str = "on-error"
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
        if self.capture_calls and (not self.api_base or self.network != "host" or self.supports_websockets or self.wire_trace_mode == "off"):
            raise ValueError("Codex call capture requires --api-base, host network, wire tracing and --no-codex-websockets")
        if self.wire_trace_mode not in {"off", "actf", "full"}:
            raise ValueError("Codex wire trace mode must be off|actf|full")
        if self.wire_trace_mode == "full" and not self.api_base:
            raise ValueError("Codex --wire-trace full requires --api-base")
        if self.wire_trace_mode == "full" and self.supports_websockets:
            raise ValueError("Codex tool capture requires --no-codex-websockets")
        if (
            self.wire_trace_mode != "off" and self.api_base
            and not self.supports_websockets and self.network != "host"
        ):
            raise ValueError(
                "Codex tool capture requires "
                "--network host so the container can reach the rollout-local proxy"
            )
        if self.config_path is not None and not self.config_path.is_file():
            raise FileNotFoundError(f"Codex config not found: {self.config_path}")


class CodexHarness(ContainerCommands, InteractiveHarness):
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
        self.capture = None
        self._capture_attempt = 0
        self._root_thread_id: str | None = None

    def create_session(
        self,
        request: RunRequest,
        output_dir: Path,
        attempt: int,
        *,
        collect_outputs: bool = True,
    ) -> InteractiveSession:
        return CodexSession(self, request, output_dir, attempt, collect_outputs=collect_outputs)

    def _start_wire_proxy(self, output_dir: Path, attempt: int = 1) -> None:
        self._root_thread_id = None
        self.capture = CaptureRuntime(output_dir, attempt, "codex", self.config.capture_debug) if self.config.capture_calls else None
        self._capture_attempt = attempt
        self._wire_proxy = None
        tools_path = output_dir / "tools_capture.jsonl"
        tools_path.unlink(missing_ok=True)
        if (
            self.config.wire_trace_mode == "off"
            or not self.config.api_base
            or self.config.supports_websockets
        ):
            return
        log_path = output_dir / "wire_trace.jsonl"
        log_path.unlink(missing_ok=True)
        self._wire_proxy = WireTraceProxy(
            self.config.api_base,
            log_path,
            mode="full" if self.config.wire_trace_mode == "full" or self.config.capture_calls else "tools",
            capture_recorder=self.capture.recorder if self.capture else None,
            capture_identity_header="thread-id" if self.capture else None,
            responses_tools_path=tools_path,
            encrypted_content_retries=CODEX_ENCRYPTED_CONTENT_RETRIES,
        )
        self._wire_proxy.start()

    def _stop_wire_proxy(self) -> None:
        if self._wire_proxy is None:
            return
        proxy = self._wire_proxy
        try:
            proxy.stop()
            if proxy.recording_error and proxy.responses_tools_path is not None:
                proxy.write_tools({"kind": "codex_tools_error", "error": proxy.recording_error})
            elif proxy.responses_tools_path is not None:
                proxy.write_tools({
                    "kind": "codex_tools_complete",
                    "request_count": proxy.responses_request_count,
                })
            if proxy.log_path.is_file():
                redact_jsonl_file(proxy.log_path)
        finally:
            self._wire_proxy = None

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
        args = docker_run_args(
            self.name, container, self.config, environment, request,
            extra_env={"CODEX_HOME": CODEX_HOME_CONTAINER_PATH},
        )
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
        return codex_effort(effort)

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
            if self.config.capture_calls and not self._root_thread_id:
                raise RuntimeError("Cannot resume Codex capture without a confirmed root thread")
            command.extend(["resume", self._root_thread_id] if self._root_thread_id else ["resume", "--last"])
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
        if self.config.capture_calls:
            command.extend(["--enable", "multi_agent"])
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
        if self.capture is not None:
            self._collect_trace(container, self.capture.output_dir, self._capture_attempt)
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
            "import json; "
            "rows=[x for x in rows if not json.loads(x.open().readline()).get('payload',{}).get('parent_thread_id')]; "
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
        if self.config.capture_calls:
            from roller.capture import read_jsonl
            meta = next((r.get("payload", {}) for r in read_jsonl(trace_path) if r.get("type") == "session_meta"), {})
            if isinstance(meta.get("id"), str) and not meta.get("parent_thread_id"):
                self._root_thread_id = meta["id"]
        return trace_path

    def run(self, request: RunRequest, output_dir: Path, attempt: int) -> HarnessResult:
        session = CodexSession(self, request, output_dir, attempt)
        status, error = "error", None
        try:
            session.start()
            for index, prompt in enumerate(request.task.prompts):
                remaining = request.timeout_seconds - (time.monotonic() - session.started_monotonic)
                if remaining <= 0:
                    raise TimeoutError("Codex task timed out")
                session.return_codes.append(
                    self._run_turn(
                        session.container,
                        request,
                        prompt,
                        resume=index > 0,
                        timeout_seconds=remaining,
                        log_path=session.log_path,
                    )
                )
            status = "ok"
        except (TimeoutError, subprocess.TimeoutExpired):
            status = "timeout"
            error = f"task exceeded {request.timeout_seconds}s"
        except Exception as exc:
            status = "error"
            error = str(exc)
        return session.finish(status, error, user_messages=request.task.prompts)


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
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.started_monotonic = time.monotonic()
        self.image_digest = harness._image_digest()
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
        # On failure ``finish`` is still responsible for stopping the proxy/container.
        self.harness.deadline = self.started_monotonic + self.request.timeout_seconds
        self.harness._start_wire_proxy(self.output_dir, self.attempt)
        # Mark before run: a timed-out Docker client may already have created it.
        self.container_started = True
        self.environment = self.harness._start_container(self.container, self.request)
        self.harness._prepare_workspace(self.container, self.request)
        self.harness._prepare_context_and_setup(self.container, self.request)
        self.harness._prepare_config(self.container)
        self.codex_version = self.harness._checked(
            ["exec", self.container, "codex", "--version"],
            "Codex version check",
        ).stdout.strip()
        self.started = True

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
            tools_trace=self.output_dir / "tools_capture.jsonl",
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

    def finish(
        self,
        status: str,
        error: str | None = None,
        *,
        user_messages: Sequence[str] | None = None,
    ) -> HarnessResult:
        """Collect traces and outputs, release the container and build the result.

        ``user_messages`` defaults to the messages delivered through :meth:`send`;
        :meth:`CodexHarness.run` passes the task prompts it ran directly.
        """

        if self._finished is not None:
            return self._finished
        harness, config = self.harness, self.harness.config
        task_id = self.request.task.id
        messages = list(self.messages if user_messages is None else user_messages)
        trace_path: Path | None = None
        trajectory_path: Path | None = None
        artifact_result: ArtifactCollectionResult | None = None
        output_copy_error: str | None = None
        try:
            harness._stop_wire_proxy()
        except Exception as exc:
            logger.warning("[%s] Codex wire proxy shutdown failed: %s", task_id, exc)
        if self.container_started:
            try:
                trace_path = harness._collect_trace(self.container, self.output_dir, self.attempt)
            except Exception as exc:
                logger.warning("[%s] Codex trace collection failed: %s", task_id, exc)
            if self.collect_outputs:
                try:
                    artifact_result, output_copy_error = harness._collect_outputs(
                        self.container, self.request, self.output_dir
                    )
                except Exception as exc:
                    output_copy_error = str(exc)
                    logger.warning("[%s] Codex output collection failed: %s", task_id, exc)
        if harness.capture is not None and trace_path is not None:
            retain_root_tools(harness.capture, trace_path)
        if trace_path is not None:
            try:
                trajectory_path = self.output_dir / "rollout" / "trace_ACTF.json"
                trajectory = convert_codex_trace(
                    trace_path,
                    trajectory_path,
                    user_messages=messages,
                    keep_thinking=config.keep_thinking,
                    tools_trace=self.output_dir / "tools_capture.jsonl",
                )
                if not trajectory.get("steps"):
                    raise ValueError("Codex native trace produced no assistant steps")
            except Exception as exc:
                trajectory_path = None
                if status == "ok":
                    status, error = "error", f"Codex trajectory conversion failed: {exc}"
        if status == "ok" and trace_path is None:
            status, error = "error", "Codex finished but no native session trace was found"
        if status == "ok" and output_copy_error is not None:
            status, error = "error", f"output collection failed: {output_copy_error}"
        if status == "ok" and artifact_result is not None and not artifact_result.passed:
            status, error = "error", "unsafe output files were rejected"
        capture_metadata = {}
        if harness.capture is not None:
            harness.capture.copy_native(harness.runtime, self.container, CODEX_HOME_CONTAINER_PATH + "/sessions/.", "sessions")
            capture_metadata = harness.capture.finish(status)
        retained_container: str | None = None
        if self.container_started:
            if config.keep_container_on_error and status != "ok":
                retained_container = self.container
            else:
                removed = remove_container(harness.runtime, self.container)
                if removed.returncode != 0:
                    logger.warning("[%s] failed to remove container %s", task_id, self.container)
        elapsed = time.monotonic() - self.started_monotonic
        usage = extract_codex_usage(trace_path) if trace_path is not None else dict(ZERO_USAGE)
        usage["elapsed_seconds"] = round(elapsed, 3)
        provenance = build_provenance(
            self.request,
            harness=harness.name,
            codex_version=self.codex_version,
            docker_image=config.docker_image,
            docker_image_digest=self.image_digest,
            api_base_urls=[redact_api_base(config.api_base)] if config.api_base else [],
            environment=self.environment,
            attempt=self.attempt,
            started_at=self.started_at,
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        try:
            reasoning_effort = resolve_thinking(
                self.request.task.thinking, config.reasoning_effort
            )
        except Exception:
            reasoning_effort = None
        self._finished = HarnessResult(
            status=status,
            elapsed_seconds=elapsed,
            error=error,
            usage=usage,
            trace_path=trace_path.relative_to(self.output_dir).as_posix() if trace_path else None,
            trajectory_path=trajectory_path.relative_to(self.output_dir).as_posix() if trajectory_path else None,
            metadata={
                **capture_metadata,
                "raw_trace_schema": "codex-session-jsonl",
                "codex_version": self.codex_version,
                "container": retained_container,
                "agent_return_codes": self.return_codes,
                "turns": len(messages),
                "reasoning_effort": reasoning_effort,
                "wire_trace": config.wire_trace_mode,
                "reasoning_summary": config.reasoning_summary,
                "keep_thinking": config.keep_thinking,
                "web_search": config.web_search,
                "supports_websockets": config.supports_websockets,
                "output_collection": output_collection_metadata(artifact_result, self.request, self.output_dir),
                "output_copy_error": output_copy_error,
                "attempt": self.attempt,
                "provenance": provenance,
            },
        )
        return self._finished
