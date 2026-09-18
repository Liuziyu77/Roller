from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from roller.actf import convert_openclaw_trace
from roller.artifacts import ArtifactCollectionResult, collect_output_directory
from roller.external_thinking import (
    normalize_external_thinking_mode,
    validate_external_thinking_provider,
    validate_external_thinking_trace,
)
from roller.harnesses.base import InteractiveHarness, InteractiveSession, TurnResult
from roller.harnesses.docker import DockerRuntime
from roller.models import HarnessResult, RunRequest
from roller.storage import safe_component
from roller.thinking import normalize_thinking, resolve_thinking
from roller.usage import ZERO_USAGE, extract_openclaw_usage
from roller.version import __version__
from roller.wire_trace import (
    WireTraceProxy,
    extract_wire_tools,
    redact_jsonl_file,
    rewrite_model_config,
)

logger = logging.getLogger(__name__)

DEFAULT_OPENCLAW_IMAGE = "ghcr.io/openclaw/openclaw:2026.7.1-2"
OPENCLAW_STATE_CONTAINER_PATH = "/tmp/roller-openclaw-state"
INPUTS_CONTAINER_PATH = "/roller/task/inputs"
CONTEXT_CONTAINER_PATH = "/roller/task/context"
OUTPUTS_CONTAINER_PATH = "/roller/task/outputs"
# OpenClaw diagnostics.cacheTrace output; records the assembled system prompt
# (and full request messages) for every LLM call made by the agent.
CACHE_TRACE_CONTAINER_PATH = "/tmp/roller-cache-trace.jsonl"
TRANSCRIPT_EXPORT_CONTAINER_PATH = "/tmp/roller-session-trace.jsonl"
EXTERNAL_THINKING_PLUGIN_CONTAINER_PATH = "/tmp/roller-external-thinking-plugin"
EXTERNAL_THINKING_PLUGIN_HOST_PATH = (
    Path(__file__).resolve().parents[1] / "assets" / "external_thinking"
)

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

_OPENCLAW_2026_3_11_UNSAFE_THINKING = frozenset(
    {"xhigh", "adaptive", "max", "ultra"}
)
_OPENCLAW_2026_7_1_UNSAFE_THINKING = frozenset({"ultra"})


def validate_openclaw_thinking(version: str, thinking: str | None) -> None:
    """Reject advanced levels known to be silently changed by a legacy image."""

    if (
        thinking in _OPENCLAW_2026_3_11_UNSAFE_THINKING
        and re.search(r"(?:^|\s)2026\.3\.11(?:\s|$)", version)
    ):
        raise RuntimeError(
            f"OpenClaw {version!r} cannot faithfully apply --thinking {thinking!r}; "
            "the bundled 2026.3.11 runtime predates the current advanced thinking "
            "profiles and can silently downgrade the request. Use off|minimal|low|"
            "medium|high, or select a newer OpenClaw image with --docker-image."
        )
    if (
        thinking in _OPENCLAW_2026_7_1_UNSAFE_THINKING
        and re.search(r"(?:^|\s)2026\.7\.1(?:\s|$)", version)
    ):
        raise RuntimeError(
            f"OpenClaw {version!r} does not accept --thinking {thinking!r}; "
            "this stable runtime supports off|minimal|low|medium|high|xhigh|"
            "adaptive|max. Select one of those levels or a later OpenClaw "
            "version that explicitly lists ultra in `openclaw agent --help`."
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


def prepare_model_config(
    config: Mapping[str, Any],
    model_name: str,
    *,
    proxy: WireTraceProxy | None = None,
    enable_reasoning: bool = False,
    disable_reasoning: bool = False,
    request_timeout_seconds: int | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Copy a model config and apply rollout-local transport/model settings."""
    if enable_reasoning and disable_reasoning:
        raise ValueError("enable_reasoning and disable_reasoning are mutually exclusive")
    prepared = rewrite_model_config(config, proxy) if proxy is not None else deepcopy(dict(config))
    providers = prepared.get("providers")
    if not isinstance(providers, dict):
        return prepared
    for provider in providers.values():
        models = provider.get("models") if isinstance(provider, dict) else None
        if not isinstance(models, list):
            continue
        for model in models:
            if not isinstance(model, dict):
                continue
            # Earlier local configs used tags=["reasoning"]. OpenClaw 2026.3.11
            # rejects the tags key and gates reasoning_effort on this boolean.
            tags = model.pop("tags", None)
            if isinstance(tags, list) and "reasoning" in tags:
                model.setdefault("reasoning", True)
    if "/" in model_name:
        provider_name, model_id = model_name.split("/", 1)
        provider = providers.get(provider_name)
        models = provider.get("models") if isinstance(provider, dict) else None
        if isinstance(provider, dict) and request_timeout_seconds is not None:
            provider["timeoutSeconds"] = request_timeout_seconds
        if isinstance(models, list):
            for model in models:
                if isinstance(model, dict) and model.get("id") == model_id:
                    if enable_reasoning or disable_reasoning:
                        model["reasoning"] = not disable_reasoning
                    if max_tokens is not None:
                        model["maxTokens"] = max_tokens
                    break
    return prepared


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


@dataclass(frozen=True)
class OpenClawConfig:
    docker_image: str = DEFAULT_OPENCLAW_IMAGE
    container_user: str | None = "0"
    gateway_port_start: int = 18789
    gateway_port_step: int = 10
    gateway_startup_timeout: float = 30.0
    network: str = "host"
    workspace_container_path: str = "/tmp_workspace"
    session_id: str = "roller"
    model_config: Mapping[str, Any] | None = None
    model_request_timeout_seconds: int | None = None
    model_max_tokens: int | None = None
    pass_env: tuple[str, ...] = COMMON_ENV_NAMES
    search_provider: str | None = None
    thinking: str | None = None
    external_thinking: str = "off"
    capture_cache_trace: bool = True
    wire_trace_mode: str = "actf"
    keep_thinking: bool = True
    collect_paths: tuple[str, ...] = ()
    keep_container_on_error: bool = False
    set_image_model: bool = True
    docker_binary: str = "docker"

    def __post_init__(self) -> None:
        if not self.docker_image.strip():
            raise ValueError("docker_image cannot be empty")
        if self.container_user is not None and not self.container_user.strip():
            raise ValueError("container_user must be non-empty or None")
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
        if (
            self.model_request_timeout_seconds is not None
            and self.model_request_timeout_seconds <= 0
        ):
            raise ValueError("model_request_timeout_seconds must be positive")
        if self.model_max_tokens is not None and self.model_max_tokens <= 0:
            raise ValueError("model_max_tokens must be positive")
        for path in self.collect_paths:
            if not path.startswith("/"):
                raise ValueError(f"collected container path must be absolute: {path}")
        object.__setattr__(self, "thinking", normalize_thinking(self.thinking))
        object.__setattr__(
            self,
            "external_thinking",
            normalize_external_thinking_mode(self.external_thinking),
        )
        if self.wire_trace_mode not in ("off", "actf", "full"):
            raise ValueError(f"wire_trace_mode must be off|actf|full: {self.wire_trace_mode}")


class OpenClawHarness(InteractiveHarness):
    """Run OpenClaw in one disposable Docker container per trajectory."""

    name = "openclaw"

    _MODEL_INJECT_SCRIPT = """
import json
import os
from pathlib import Path
config_path = Path(os.environ['ROLLER_OPENCLAW_CONFIG_PATH'])
models_path = Path('/tmp/roller-models.json')
config = json.loads(config_path.read_text()) if config_path.exists() else {}
config['models'] = json.loads(models_path.read_text())
config_path.parent.mkdir(parents=True, exist_ok=True)
config_path.write_text(json.dumps(config, indent=2))
""".strip()

    _EXTERNAL_THINKING_PLUGIN_INJECT_SCRIPT = """
import json
import os
from pathlib import Path
config_path = Path(os.environ['ROLLER_OPENCLAW_CONFIG_PATH'])
plugin_path = os.environ['ROLLER_EXTERNAL_THINKING_PLUGIN_PATH']
config = json.loads(config_path.read_text()) if config_path.exists() else {}
plugins = config.setdefault('plugins', {})
load_paths = plugins.setdefault('load', {}).setdefault('paths', [])
if plugin_path not in load_paths:
    load_paths.append(plugin_path)
allow = plugins.setdefault('allow', [])
if 'roller-external-thinking' not in allow:
    allow.append('roller-external-thinking')
plugins.setdefault('entries', {}).setdefault(
    'roller-external-thinking', {'enabled': True}
)['enabled'] = True
config_path.parent.mkdir(parents=True, exist_ok=True)
config_path.write_text(json.dumps(config, indent=2))
""".strip()

    _AUTH_INJECT_SCRIPT = """
import json
import os
from pathlib import Path
path = Path(os.environ['ROLLER_OPENCLAW_AUTH_PATH'])
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
            shutil.copyfile(source, Path.home() / name)
""".strip()

    _LATEST_SESSION_SCRIPT = """
import os
from pathlib import Path
roots = [
    Path(os.environ['OPENCLAW_STATE_DIR']) / 'agents/main/sessions',
    Path('/root/.openclaw/agents/main/sessions'),
]
files = sorted(
    {path for root in roots for path in root.glob('*.jsonl')},
    key=lambda path: path.stat().st_mtime,
    reverse=True,
)
print(files[0] if files else '')
""".strip()

    _LEGACY_LAST_ASSISTANT_SCRIPT = """
# ROLLER_LEGACY_LAST_ASSISTANT
import json
import os
from pathlib import Path
roots = [
    Path(os.environ['OPENCLAW_STATE_DIR']) / 'agents/main/sessions',
    Path('/root/.openclaw/agents/main/sessions'),
]
preferred = [root / __ROLLER_SESSION_FILE__ for root in roots]
files = [path for path in preferred if path.is_file()]
if not files:
    files = sorted(
        {path for root in roots for path in root.glob('*.jsonl')},
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
content = ''
assistant_count = 0
stop_reason = ''
error_message = ''
if files:
    rows = []
    for line in files[0].read_text(errors='replace').splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    assistant_count = sum(
        isinstance(row, dict)
        and isinstance(row.get('message'), dict)
        and row['message'].get('role') == 'assistant'
        for row in rows
    )
    for row in reversed(rows):
        message = row.get('message') if isinstance(row, dict) else None
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            continue
        stop_reason = message.get('stopReason', message.get('stop_reason', ''))
        error_message = message.get('errorMessage', message.get('error_message', ''))
        value = message.get('content')
        if isinstance(value, str):
            content = value
        elif isinstance(value, list):
            content = '\\n'.join(
                block.get('text', '')
                for block in value
                if isinstance(block, dict) and block.get('type') == 'text'
            )
        elif isinstance(value, dict) and isinstance(value.get('text'), str):
            content = value['text']
        break
print(json.dumps({
    'content': content,
    'assistant_count': assistant_count,
    'stop_reason': stop_reason,
    'error': error_message,
}, ensure_ascii=False))
""".strip()

    _SDK_LAST_ASSISTANT_SCRIPT = """
// ROLLER_SDK_LAST_ASSISTANT
const scope = JSON.parse(process.env.ROLLER_SESSION_SCOPE);
const { readLatestAssistantTextByIdentity, readSessionTranscriptEvents } =
  await import('openclaw/plugin-sdk/session-transcript-runtime');
const events = await readSessionTranscriptEvents(scope);
const latest = await readLatestAssistantTextByIdentity(scope);
const assistants = events.filter((event) =>
  event && typeof event === 'object' && event.message &&
  typeof event.message === 'object' && event.message.role === 'assistant'
);
const last = assistants.length ? assistants[assistants.length - 1].message : {};
const blocks = last?.content;
let content = '';
if (typeof blocks === 'string') {
  content = blocks;
} else if (Array.isArray(blocks)) {
  content = blocks
    .filter((block) => block && typeof block === 'object' && block.type === 'text')
    .map((block) => typeof block.text === 'string' ? block.text : '')
    .join('\\n');
} else if (blocks && typeof blocks === 'object' && typeof blocks.text === 'string') {
  content = blocks.text;
}
process.stdout.write(JSON.stringify({
  content,
  assistant_count: assistants.length,
  stop_reason: last?.stopReason ?? last?.stop_reason ?? '',
  error: last?.errorMessage ?? last?.error_message ?? '',
}));
""".strip()

    _SDK_EXPORT_TRANSCRIPT_SCRIPT = """
// ROLLER_SDK_EXPORT_TRANSCRIPT
import fs from 'node:fs';
const scope = JSON.parse(process.env.ROLLER_SESSION_SCOPE);
const { readSessionTranscriptEvents } =
  await import('openclaw/plugin-sdk/session-transcript-runtime');
const events = await readSessionTranscriptEvents(scope);
const payload = events.map((event) => JSON.stringify(event)).join('\\n');
fs.writeFileSync(process.env.ROLLER_TRACE_OUTPUT, payload ? `${payload}\\n` : '');
process.stdout.write(JSON.stringify({ event_count: events.length }));
""".strip()

    _OPENCLAW_NODE_EVAL_SCRIPT = """
runtime_dir=$(dirname "$(readlink -f "$(command -v openclaw)")")
cd "$runtime_dir"
exec node --input-type=module -e "$1"
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
        self._wire_proxy: WireTraceProxy | None = None

    def _start_wire_proxy(self, request: RunRequest, output_dir: Path) -> None:
        """Start the wire-trace proxy for this rollout.

        The proxy runs when wire tracing is active, when thinking must be
        stripped from request history, when Chat Completions reasoning fields
        need normalization, or when Responses external thinking is enabled.
        """
        self._wire_proxy = None
        needs_proxy = (
            self.config.wire_trace_mode != "off"
            or not self.config.keep_thinking
            or self.config.model_config is not None
        )
        if not needs_proxy:
            return
        external_thinking = self.config.external_thinking != "off"
        validate_external_thinking_provider(
            self.config.model_config,
            request.model,
            self.config.external_thinking,
        )
        proxy_required = (
            self.config.wire_trace_mode == "full"
            or not self.config.keep_thinking
            or external_thinking
        )
        if self.config.model_config is None:
            if proxy_required:
                raise RuntimeError(
                    "wire tracing/stripping requires --model-config with the selected "
                    "provider's baseUrl"
                )
            logger.debug(
                "[%s] tools schema capture skipped: no model config was provided",
                request.task.id,
            )
            return
        providers = self.config.model_config.get("providers")
        provider_name = request.model.split("/", 1)[0]
        provider = providers.get(provider_name) if isinstance(providers, Mapping) else None
        upstream = provider.get("baseUrl") if isinstance(provider, Mapping) else None
        if not isinstance(upstream, str) or not upstream.strip():
            if proxy_required:
                raise RuntimeError(
                    f"wire tracing/stripping requires baseUrl for provider {provider_name!r}"
                )
            logger.debug(
                "[%s] tools schema capture skipped: provider %r has no baseUrl",
                request.task.id,
                provider_name,
            )
            return
        if self.config.wire_trace_mode == "full":
            log_path = output_dir / "wire_trace.jsonl"
            log_mode = "full"
        elif self.config.wire_trace_mode == "actf":
            log_path = output_dir / "wire_trace.jsonl"
            log_mode = "tools"
        else:  # Internal-only proxy; external thinking still needs tools for SFT.
            log_path = (
                output_dir / "wire_trace.jsonl"
                if external_thinking
                else output_dir / "rollout" / "wire_strip.jsonl"
            )
            log_mode = "tools"
        try:
            self._wire_proxy = WireTraceProxy(
                upstream,
                log_path,
                mode=log_mode,
                strip_thinking=not self.config.keep_thinking,
                normalize_reasoning_replay=self.config.keep_thinking,
                external_thinking_mode=self.config.external_thinking,
            )
            self._wire_proxy.start()
        except Exception as exc:
            if self._wire_proxy is not None:
                self._stop_wire_proxy()
            if proxy_required:
                raise RuntimeError(f"wire trace proxy failed to start: {exc}") from exc
            logger.warning("[%s] tools schema capture skipped: %s", request.task.id, exc)
            self._wire_proxy = None

    def _stop_wire_proxy(self) -> None:
        if self._wire_proxy is not None:
            try:
                self._wire_proxy.stop()
            except Exception as exc:
                logger.warning("wire trace proxy shutdown failed: %s", exc)
            self._wire_proxy = None

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
        if self.config.container_user is not None:
            args.extend(["--user", self.config.container_user])
        if self.config.network:
            args.extend(["--network", self.config.network])
        for key, value in sorted(environment.items()):
            args.extend(["-e", f"{key}={value}"])
        # Keep Roller independent of the image's default user/HOME. Official
        # OpenClaw images run as ``node`` while legacy benchmark images run as
        # ``root``; both can write this per-container state directory.
        args.extend(["-e", f"OPENCLAW_STATE_DIR={OPENCLAW_STATE_CONTAINER_PATH}"])

        if request.bundle is not None:
            args.extend(["-v", f"{request.bundle.inputs_root}:{INPUTS_CONTAINER_PATH}:ro"])
            args.extend(["-v", f"{request.bundle.context_root}:{CONTEXT_CONTAINER_PATH}:ro"])
            if not request.bundle.legacy_workspace:
                for input_file in request.task.inputs:
                    input_path = request.bundle.inputs_root / input_file.path
                    if not input_path.exists():
                        continue
                    args.extend(
                        [
                            "-v",
                            f"{input_path}:"
                            f"{self.config.workspace_container_path}/{input_file.path}:ro",
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
        output_root = f"{workspace}/{request.task.output_dir}"
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
        quoted_output_root = shlex.quote(output_root)
        script = (
            f"{setup} && rm -rf {quoted_output_root} && mkdir -p {quoted_output_root} && "
            f"mkdir -p /roller/task && rm -rf {OUTPUTS_CONTAINER_PATH} && "
            f"ln -s {quoted_output_root} {OUTPUTS_CONTAINER_PATH}"
        )
        self._checked(["exec", container, "/bin/bash", "-c", script], "workspace setup")
        link_script = (
            f"mkdir -p {OPENCLAW_STATE_CONTAINER_PATH} /root/.openclaw && "
            f"rm -rf {OPENCLAW_STATE_CONTAINER_PATH}/workspace && "
            f"ln -s {workspace} {OPENCLAW_STATE_CONTAINER_PATH}/workspace && "
            "rm -rf /root/.openclaw/workspace && "
            f"ln -s {workspace} /root/.openclaw/workspace"
        )
        self._checked(["exec", container, "/bin/bash", "-c", link_script], "OpenClaw workspace link")

    @staticmethod
    def _state_dir_for_version(version: str) -> str:
        # OpenClaw 2026.3.11 predates OPENCLAW_STATE_DIR. Keep its baked image
        # and JSONL compatibility path while newer runtimes use isolated state.
        if re.search(r"(?:^|\s)2026\.3\.11(?:\s|$)", version):
            return "/root/.openclaw"
        return OPENCLAW_STATE_CONTAINER_PATH

    def _effective_thinking(self, request: RunRequest) -> str | None:
        if self.config.external_thinking != "off":
            return "off"
        return resolve_thinking(request.task.thinking, self.config.thinking)

    def _inject_external_thinking_plugin(
        self,
        container: str,
        state_dir: str,
    ) -> None:
        if self.config.external_thinking == "off":
            return
        entry_path = EXTERNAL_THINKING_PLUGIN_HOST_PATH / "index.cjs"
        manifest_path = EXTERNAL_THINKING_PLUGIN_HOST_PATH / "openclaw.plugin.json"
        if not entry_path.is_file() or not manifest_path.is_file():
            raise RuntimeError(
                "Roller external thinking plugin assets are missing from the installation"
            )
        self._checked(
            [
                "cp",
                f"{EXTERNAL_THINKING_PLUGIN_HOST_PATH}/.",
                f"{container}:{EXTERNAL_THINKING_PLUGIN_CONTAINER_PATH}",
            ],
            "external thinking plugin copy",
        )
        self._checked(
            [
                "exec",
                "--user",
                "0",
                container,
                "/bin/sh",
                "-c",
                "owner=$(stat -c %u:%g /proc/1) && "
                f"chown -R \"$owner\" {EXTERNAL_THINKING_PLUGIN_CONTAINER_PATH}",
            ],
            "external thinking plugin ownership",
        )
        self._checked(
            [
                "exec",
                "-e",
                f"ROLLER_OPENCLAW_CONFIG_PATH={state_dir}/openclaw.json",
                "-e",
                "ROLLER_EXTERNAL_THINKING_PLUGIN_PATH="
                f"{EXTERNAL_THINKING_PLUGIN_CONTAINER_PATH}/index.cjs",
                container,
                "python3",
                "-c",
                self._EXTERNAL_THINKING_PLUGIN_INJECT_SCRIPT,
            ],
            "external thinking plugin config injection",
        )

    def _inject_models(
        self,
        container: str,
        request: RunRequest,
        state_dir: str,
    ) -> None:
        if self.config.model_config is None:
            return
        thinking = self._effective_thinking(request)
        config_to_write = prepare_model_config(
            self.config.model_config,
            request.model,
            proxy=self._wire_proxy,
            enable_reasoning=thinking not in (None, "off"),
            disable_reasoning=self.config.external_thinking != "off",
            request_timeout_seconds=self.config.model_request_timeout_seconds,
            max_tokens=self.config.model_max_tokens,
        )
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", suffix=".json", delete=False
            ) as stream:
                json.dump(config_to_write, stream, ensure_ascii=False)
                temporary_path = Path(stream.name)
            self._checked(
                ["cp", str(temporary_path), f"{container}:/tmp/roller-models.json"],
                "model config copy",
            )
            self._checked(
                [
                    "exec",
                    "-e",
                    f"ROLLER_OPENCLAW_CONFIG_PATH={state_dir}/openclaw.json",
                    container,
                    "python3",
                    "-c",
                    self._MODEL_INJECT_SCRIPT,
                ],
                "model config injection",
            )
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _configure(self, container: str, request: RunRequest, environment: Mapping[str, str]) -> str:
        version = self._checked(["exec", container, "openclaw", "--version"], "OpenClaw version").stdout.strip()
        state_dir = self._state_dir_for_version(version)
        thinking = self._effective_thinking(request)
        validate_openclaw_thinking(version, thinking)
        if self.config.external_thinking != "off" and re.search(
            r"(?:^|\s)2026\.3\.11(?:\s|$)", version
        ):
            raise RuntimeError(
                "external thinking requires OpenClaw 2026.7.1 or newer because "
                "the legacy 2026.3.11 image lacks the required plugin contract"
            )
        self._inject_models(container, request, state_dir)
        self._inject_external_thinking_plugin(container, state_dir)
        self._checked(
            ["exec", container, "openclaw", "models", "set", request.model],
            "model setup",
        )
        self._checked(
            ["exec", container, "openclaw", "config", "set", "gateway.port", str(self.gateway_port)],
            "gateway port setup",
        )
        self._checked(
            ["exec", container, "openclaw", "config", "set", "gateway.mode", "local"],
            "gateway mode setup",
        )
        self._checked(
            ["exec", container, "openclaw", "config", "set", "gateway.bind", "loopback"],
            "gateway bind setup",
        )
        self._checked(
            [
                "exec",
                container,
                "openclaw",
                "config",
                "set",
                "agents.defaults.workspace",
                self.config.workspace_container_path,
            ],
            "OpenClaw workspace setup",
        )
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
        if self.config.capture_cache_trace:
            # Persist the fully assembled system prompt (per LLM call) via
            # OpenClaw's diagnostics.cacheTrace so downstream consumers never
            # need to reconstruct or hardcode it.
            self._checked(
                [
                    "exec",
                    container,
                    "openclaw",
                    "config",
                    "set",
                    "diagnostics.cacheTrace.enabled",
                    "true",
                ],
                "cache trace setup",
            )
            self._checked(
                [
                    "exec",
                    container,
                    "openclaw",
                    "config",
                    "set",
                    "diagnostics.cacheTrace.filePath",
                    CACHE_TRACE_CONTAINER_PATH,
                ],
                "cache trace path setup",
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
                    "-e",
                    f"ROLLER_OPENCLAW_AUTH_PATH={state_dir}/agents/main/agent/auth-profiles.json",
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
                    f"cp -a {CONTEXT_CONTAINER_PATH}/. \"$HOME\"/ && "
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
            try:
                result = self.runtime.run(["exec", container, "python3", "-c", probe], timeout=2)
            except subprocess.TimeoutExpired:
                # Docker exec can be slow while other Harness containers start.
                # A single probe timeout must not discard the startup budget.
                continue
            if result.returncode == 0:
                return
            time.sleep(0.25)
        raise TimeoutError(
            f"OpenClaw gateway did not listen on port {self.gateway_port} within "
            f"{self.config.gateway_startup_timeout:g}s"
        )

    def _prompts(self, request: RunRequest) -> tuple[str, ...]:
        return request.task.prompts

    def _session_scope(self) -> dict[str, str]:
        session_id = self.config.session_id
        return {
            "agentId": "main",
            "sessionId": session_id,
            "sessionKey": f"agent:main:explicit:{session_id}",
        }

    def _run_transcript_sdk(
        self,
        container: str,
        script: str,
        *,
        output_path: str | None = None,
        state_dir: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        args = [
            "exec",
            "-e",
            f"ROLLER_SESSION_SCOPE={json.dumps(self._session_scope(), separators=(',', ':'))}",
        ]
        if output_path is not None:
            args.extend(["-e", f"ROLLER_TRACE_OUTPUT={output_path}"])
        if state_dir is not None:
            # OpenClaw 2026.3.11 ignores OPENCLAW_STATE_DIR for gateway
            # commands but the transcript SDK follows it. Keep both sides on
            # the same state directory when using the legacy image.
            args.extend(["-e", f"OPENCLAW_STATE_DIR={state_dir}"])
        args.extend(
            [
                container,
                "/bin/sh",
                "-lc",
                self._OPENCLAW_NODE_EVAL_SCRIPT,
                "roller-transcript-sdk",
                script,
            ]
        )
        return self.runtime.run(args)

    def _latest_assistant_content(
        self, container: str, *, state_dir: str | None = None
    ) -> tuple[str, int]:
        result = self._run_transcript_sdk(
            container, self._SDK_LAST_ASSISTANT_SCRIPT, state_dir=state_dir
        )
        if result.returncode != 0:
            logger.debug(
                "OpenClaw transcript SDK unavailable; using legacy JSONL response reader: %s",
                self._error_text(result),
            )
            script = self._LEGACY_LAST_ASSISTANT_SCRIPT.replace(
                "__ROLLER_SESSION_FILE__", repr(f"{self.config.session_id}.jsonl")
            )
            result = self._checked(
                ["exec", container, "python3", "-c", script],
                "assistant response extraction",
            )
        try:
            payload = json.loads(result.stdout.strip())
        except json.JSONDecodeError as exc:
            raise RuntimeError("assistant response extraction returned invalid JSON") from exc
        content = payload.get("content") if isinstance(payload, dict) else None
        assistant_count = (
            payload.get("assistant_count") if isinstance(payload, dict) else None
        )
        stop_reason = payload.get("stop_reason") if isinstance(payload, dict) else None
        if stop_reason in {"aborted", "error"}:
            detail = payload.get("error") if isinstance(payload, dict) else None
            raise RuntimeError(
                detail if isinstance(detail, str) and detail else f"OpenClaw {stop_reason}"
            )
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("OpenClaw turn produced no visible assistant response")
        if isinstance(assistant_count, bool) or not isinstance(assistant_count, int):
            raise RuntimeError("assistant response extraction returned invalid count")
        return content.strip(), assistant_count

    def _collect_trace(
        self,
        container: str,
        output_dir: Path,
        attempt: int,
        *,
        state_dir: str | None = None,
    ) -> Path | None:
        rollout_dir = output_dir / "rollout"
        rollout_dir.mkdir(parents=True, exist_ok=True)
        attempt_trace_path = rollout_dir / f"trace.attempt-{attempt}.jsonl"
        trace_path = rollout_dir / "trace.jsonl"
        attempt_trace_path.unlink(missing_ok=True)

        sdk_result = self._run_transcript_sdk(
            container,
            self._SDK_EXPORT_TRANSCRIPT_SCRIPT,
            output_path=TRANSCRIPT_EXPORT_CONTAINER_PATH,
            state_dir=state_dir,
        )
        if sdk_result.returncode == 0:
            result = self.runtime.run(
                [
                    "cp",
                    f"{container}:{TRANSCRIPT_EXPORT_CONTAINER_PATH}",
                    str(attempt_trace_path),
                ]
            )
        else:
            logger.debug(
                "OpenClaw transcript SDK unavailable; copying legacy JSONL trace: %s",
                self._error_text(sdk_result),
            )
            container_path = (
                f"{OPENCLAW_STATE_CONTAINER_PATH}/agents/main/sessions/"
                f"{self.config.session_id}.jsonl"
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
        if (
            result.returncode != 0
            or not attempt_trace_path.is_file()
            or attempt_trace_path.stat().st_size == 0
        ):
            return None
        shutil.copyfile(attempt_trace_path, trace_path)
        return trace_path

    def _collect_cache_trace(self, container: str, output_dir: Path) -> Path | None:
        """Copy the OpenClaw cache-trace file (system prompts per LLM call) into the sample."""
        destination = output_dir / "cache_trace.jsonl"
        if not self.config.capture_cache_trace:
            return None
        destination.unlink(missing_ok=True)
        result = self.runtime.run(
            ["cp", f"{container}:{CACHE_TRACE_CONTAINER_PATH}", str(destination)]
        )
        if result.returncode != 0 or not destination.is_file():
            return None
        try:
            redact_jsonl_file(destination)
        except Exception:
            # Full mode retains this file. Never leave an unsanitized credential-
            # bearing cache trace behind when the redaction pass fails.
            destination.unlink(missing_ok=True)
            raise
        return destination

    def _collect_legacy_artifacts(self, container: str, output_dir: Path) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        artifact_root = output_dir / "results" / "legacy"
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

    def create_session(
        self,
        request: RunRequest,
        output_dir: Path,
        attempt: int,
        *,
        collect_outputs: bool = True,
    ) -> InteractiveSession:
        return OpenClawSession(
            self,
            request,
            output_dir,
            attempt,
            collect_outputs=collect_outputs,
        )

    def run(self, request: RunRequest, output_dir: Path, attempt: int) -> HarnessResult:
        session = self.create_session(request, output_dir, attempt)
        status = "error"
        error: str | None = None
        started = time.monotonic()
        try:
            session.start()
            for prompt in self._prompts(request):
                remaining = request.timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError(f"task exceeded {request.timeout_seconds}s")
                session.send(prompt, remaining)
            status = "ok"
        except (TimeoutError, subprocess.TimeoutExpired):
            status = "timeout"
            error = f"task exceeded {request.timeout_seconds}s"
        except Exception as exc:
            status = "error"
            error = str(exc)
        return session.finish(status, error)


class OpenClawSession(InteractiveSession):
    """One live OpenClaw container used turn-by-turn by a conversation manager."""

    def __init__(
        self,
        harness: OpenClawHarness,
        request: RunRequest,
        output_dir: Path,
        attempt: int,
        *,
        collect_outputs: bool,
    ) -> None:
        self.harness = harness
        self.request = request
        self.output_dir = output_dir
        self.attempt = attempt
        self.collect_outputs = collect_outputs
        self.started = time.monotonic()
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.container = (
            f"roller-{request.index}-{safe_component(request.model, 24)}-"
            f"{uuid.uuid4().hex[:8]}"
        ).lower()
        self.gateway_token = uuid.uuid4().hex
        self.gateway_process: subprocess.Popen[str] | None = None
        self.container_started = False
        self.version = ""
        self.state_dir = OPENCLAW_STATE_CONTAINER_PATH
        self.return_codes: list[int] = []
        self.messages: list[str] = []
        self.environment: dict[str, str] = {}
        self.image_digest = harness._image_digest()
        self._assistant_count = 0
        self._finished: HarnessResult | None = None

    def start(self) -> None:
        if self.container_started:
            raise RuntimeError("OpenClaw session has already started")
        # Retries share one sample directory. Every trace layer must represent
        # the current attempt rather than a previous partial session.
        for relative in (
            "rollout/trace.jsonl",
            "cache_trace.jsonl",
            "wire_trace.jsonl",
            "rollout/wire_strip.jsonl",
        ):
            (self.output_dir / relative).unlink(missing_ok=True)
        self.harness._start_wire_proxy(self.request, self.output_dir)
        self.environment = self.harness._start_container(self.container, self.request)
        self.container_started = True
        self.harness._prepare_workspace(self.container, self.request)
        self.harness._prepare_context_and_setup(self.container, self.request)
        self.version = self.harness._configure(
            self.container, self.request, self.environment
        )
        self.state_dir = self.harness._state_dir_for_version(self.version)
        # The legacy 2026.3.11 image keeps its gateway config under
        # /root/.openclaw while the temporary OPENCLAW_STATE_DIR is used by
        # the CLI. Set both token locations explicitly so gateway and agent
        # clients authenticate with the same per-rollout token.
        for setting in ("gateway.auth.token", "gateway.remote.token"):
            self.harness._checked(
                [
                    "exec",
                    self.container,
                    "openclaw",
                    "config",
                    "set",
                    setting,
                    self.gateway_token,
                ],
                f"gateway token setup ({setting})",
            )
        self.gateway_process = self.harness.runtime.popen(
            [
                "exec",
                "-e",
                f"OPENCLAW_GATEWAY_TOKEN={self.gateway_token}",
                "-e",
                f"OPENCLAW_STATE_DIR={self.state_dir}",
                "-w",
                self.harness.config.workspace_container_path,
                self.container,
                "openclaw",
                "gateway",
                "--port",
                str(self.harness.gateway_port),
            ],
            self.output_dir / "rollout" / f"gateway.attempt-{self.attempt}.log",
        )
        self.harness._wait_for_gateway(self.container, self.gateway_process)

    def send(self, message: str, timeout_seconds: float) -> TurnResult:
        if not self.container_started or self.gateway_process is None:
            raise RuntimeError("OpenClaw session is not running")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("interactive message must be non-empty")
        if timeout_seconds <= 0:
            raise TimeoutError("interactive turn has no time remaining")
        turn_started = time.monotonic()
        command = [
            "exec",
            "-e",
            f"OPENCLAW_GATEWAY_TOKEN={self.gateway_token}",
            "-e",
            f"OPENCLAW_STATE_DIR={self.state_dir}",
            "-w",
            self.harness.config.workspace_container_path,
            self.container,
            "openclaw",
            "agent",
            "--session-id",
            self.harness.config.session_id,
            "--timeout",
            str(max(1, int(timeout_seconds))),
        ]
        thinking = self.harness._effective_thinking(self.request)
        if thinking:
            # The CLI flag reaches this live session; changing the default after
            # gateway startup does not reliably affect an existing agent turn.
            command += ["--thinking", thinking]
        command += ["--message", message]
        agent_log_path = (
            self.output_dir / "rollout" / f"agent.attempt-{self.attempt}.log"
        )
        process = self.harness.runtime.popen(
            command,
            agent_log_path,
            append=bool(self.messages),
        )
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            self.harness.runtime.run(
                ["exec", self.container, "pkill", "-f", "openclaw agent"]
            )
            raise TimeoutError("OpenClaw interactive turn timed out") from exc
        finally:
            self.harness.runtime.close_log(process)
        self.return_codes.append(return_code)
        if return_code != 0:
            detail = ""
            try:
                detail = agent_log_path.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()[-2000:]
            except OSError:
                pass
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(
                f"OpenClaw agent turn exited with code {return_code}{suffix}"
            )
        content, assistant_count = self.harness._latest_assistant_content(
            self.container, state_dir=self.state_dir
        )
        if assistant_count <= self._assistant_count:
            raise RuntimeError("OpenClaw turn produced no new assistant response")
        self._assistant_count = assistant_count
        self.messages.append(message)
        return TurnResult(content, time.monotonic() - turn_started, return_code)

    def finish(self, status: str, error: str | None = None) -> HarnessResult:
        if self._finished is not None:
            return self._finished
        trace_path: Path | None = None
        trajectory_path: Path | None = None
        legacy_records: list[dict[str, Any]] = []
        artifact_result: ArtifactCollectionResult | None = None
        output_copy_error: str | None = None
        external_thinking_metadata: dict[str, Any] = {
            "mode": self.harness.config.external_thinking,
            "native_reasoning": (
                "off" if self.harness.config.external_thinking != "off" else None
            ),
            "validated": self.harness.config.external_thinking == "off",
            "deep_think_calls": 0,
            "thought_action_pairs": 0,
            "validation_error": None,
        }
        if self.container_started:
            try:
                trace_path = self.harness._collect_trace(
                    self.container,
                    self.output_dir,
                    self.attempt,
                    state_dir=self.state_dir,
                )
            except Exception as exc:
                logger.warning("[%s] trace collection failed: %s", self.request.task.id, exc)
            try:
                self.harness._collect_cache_trace(self.container, self.output_dir)
            except Exception as exc:
                logger.warning(
                    "[%s] cache trace collection failed: %s",
                    self.request.task.id,
                    exc,
                )
            if self.collect_outputs:
                try:
                    artifact_result, output_copy_error = self.harness._collect_outputs(
                        self.container, self.request, self.output_dir
                    )
                except Exception as exc:
                    output_copy_error = str(exc)
                    logger.warning(
                        "[%s] output collection failed: %s", self.request.task.id, exc
                    )
                try:
                    legacy_records = self.harness._collect_legacy_artifacts(
                        self.container, self.output_dir
                    )
                except Exception as exc:
                    logger.warning(
                        "[%s] legacy artifact collection failed: %s",
                        self.request.task.id,
                        exc,
                    )
        if (
            trace_path is not None
            and self.harness.config.external_thinking != "off"
        ):
            try:
                external_stats = validate_external_thinking_trace(
                    trace_path,
                    self.harness.config.external_thinking,
                )
                external_thinking_metadata.update(
                    {
                        "validated": True,
                        "deep_think_calls": external_stats.deep_think_calls,
                        "thought_action_pairs": external_stats.thought_action_pairs,
                    }
                )
            except ValueError as exc:
                external_thinking_metadata["validation_error"] = str(exc)
                if status == "ok":
                    status = "error"
                    error = f"external thinking protocol validation failed: {exc}"
        try:
            try:
                self.harness._stop_process(self.gateway_process)
            finally:
                self.harness.runtime.close_log(self.gateway_process)
        finally:
            self.harness._stop_wire_proxy()

        if trace_path is not None:
            try:
                cache_trace_path = self.output_dir / "cache_trace.jsonl"
                wire_trace_path = self.output_dir / "wire_trace.jsonl"
                trajectory_path = self.output_dir / "rollout" / "trace_ACTF.json"
                convert_openclaw_trace(
                    trace_path,
                    trajectory_path,
                    user_messages=self.messages,
                    cache_trace=(
                        cache_trace_path if cache_trace_path.is_file() else None
                    ),
                    tools=(
                        extract_wire_tools(wire_trace_path)
                        if wire_trace_path.is_file()
                        else None
                    ),
                    keep_thinking=self.harness.config.keep_thinking,
                )
            except Exception as exc:
                trajectory_path = None
                if status == "ok":
                    status = "error"
                    error = f"OpenClaw trajectory conversion failed: {exc}"

        if status == "ok" and trace_path is None:
            status = "error"
            error = "OpenClaw finished but no session trace was found"
        if status == "ok" and output_copy_error is not None:
            status = "error"
            error = f"output collection failed: {output_copy_error}"
        if status == "ok" and artifact_result is not None and not artifact_result.passed:
            status = "error"
            error = "unsafe output files were rejected"
        if self.container_started and not (
            self.harness.config.keep_container_on_error and status != "ok"
        ):
            try:
                remove_result = self.harness.runtime.run(
                    ["rm", "-f", self.container], timeout=30
                )
                if remove_result.returncode != 0:
                    logger.warning(
                        "[%s] failed to remove container %s",
                        self.request.task.id,
                        self.container,
                    )
            except Exception as exc:
                logger.warning(
                    "[%s] failed to remove container %s: %s",
                    self.request.task.id,
                    self.container,
                    exc,
                )

        elapsed = time.monotonic() - self.started
        usage: dict[str, Any] = (
            extract_openclaw_usage(trace_path)
            if trace_path is not None
            else dict(ZERO_USAGE)
        )
        usage["elapsed_seconds"] = round(elapsed, 3)
        api_base_urls = _model_base_urls(self.harness.config.model_config or {})
        if self.environment.get("OPENROUTER_BASE_URL"):
            api_base_urls.append(_redact_url(self.environment["OPENROUTER_BASE_URL"]))
        provenance = {
            "schema_version": "roller.provenance/v1",
            "roller_version": __version__,
            "task_id": self.request.task.id,
            "task_bundle_digest": (
                self.request.bundle.manifest_digest if self.request.bundle else None
            ),
            "input_tree_digest": (
                self.request.bundle.input_tree_digest if self.request.bundle else None
            ),
            "input_manifest_digest": (
                self.request.bundle.input_manifest_digest if self.request.bundle else None
            ),
            "context_manifest_digest": (
                self.request.bundle.context_manifest_digest if self.request.bundle else None
            ),
            "resume_safe": self.request.bundle.resume_safe if self.request.bundle else None,
            "harness": self.harness.name,
            "openclaw_version": self.version,
            "docker_image": self.harness.config.docker_image,
            "docker_image_digest": self.image_digest,
            "model": self.request.model,
            "external_thinking": self.harness.config.external_thinking,
            "api_base_urls": list(dict.fromkeys(api_base_urls)),
            "environment_names": sorted(self.environment),
            "attempt": self.attempt,
            "started_at": self.started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        output_metadata = (
            {
                "mode": "all",
                "agent_output_dir": self.request.task.output_dir,
                "passed": artifact_result.passed,
                "path": (
                    artifact_result.results_path.relative_to(self.output_dir).as_posix()
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
        self._finished = HarnessResult(
            status=status,
            elapsed_seconds=elapsed,
            error=error,
            usage=usage,
            trace_path=(
                trace_path.relative_to(self.output_dir).as_posix()
                if trace_path is not None
                else None
            ),
            trajectory_path=(
                trajectory_path.relative_to(self.output_dir).as_posix()
                if trajectory_path is not None
                else None
            ),
            metadata={
                "raw_trace_schema": "openclaw-session-jsonl",
                "openclaw_version": self.version,
                "gateway_port": self.harness.gateway_port,
                "container": (
                    self.container
                    if self.harness.config.keep_container_on_error and status != "ok"
                    else None
                ),
                "agent_return_codes": self.return_codes,
                "turns": len(self.messages),
                "external_thinking": external_thinking_metadata,
                "output_collection": output_metadata,
                "output_copy_error": output_copy_error,
                "legacy_artifacts": legacy_records,
                "attempt": self.attempt,
                "provenance": provenance,
            },
        )
        return self._finished
