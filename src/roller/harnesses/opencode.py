from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from roller.harnesses.bridge import BridgeHarness, BridgeSession, validate_bridge_config, validate_step_limits
from roller.harnesses.container import PROXY_ENV_NAMES as COMMON_ENV_NAMES
from roller.models import RunRequest
from roller.opencode_actf import convert_opencode_trace, extract_opencode_usage
from roller.thinking import normalize_thinking, resolve_thinking

OPENCODE_VERSION = "1.18.30"
DEFAULT_OPENCODE_IMAGE = "roller-opencode:1.18.30"
RUNNER_PATH = Path(__file__).resolve().parents[1] / "assets" / "opencode" / "runner.py"
STATE = "/roller/opencode-state"
RUNNER = "/roller/opencode_runner.py"
PLUGIN = "/roller/opencode_journal.mjs"
OPENCODE_TOOLS = (
    "bash",
    "read",
    "write",
    "edit",
    "apply_patch",
    "glob",
    "grep",
    "skill",
)
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
    wire_trace_mode: str = "actf"
    capture_calls: bool = False
    capture_debug: str = "on-error"
    keep_thinking: bool = True
    keep_container_on_error: bool = False
    docker_binary: str = "docker"

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasoning_effort", opencode_effort(self.reasoning_effort))
        validate_bridge_config(self, "OpenCode")
        validate_step_limits(self, "OpenCode")


class OpenCodeSession(BridgeSession):
    label = "OpenCode"
    state_dir = STATE
    runner = RUNNER
    runner_source = RUNNER_PATH
    raw_trace_schema = "roller.opencode-events/v1"
    assets = (("journal.mjs", PLUGIN, "journal plugin copy"),)
    native_trace_name = "journal.jsonl"
    conversion_failure_is_error = False
    redact_capture_errors = True
    supports_reset = True

    def effort(self) -> str | None:
        return opencode_effort(resolve_thinking(self.request.task.thinking, self.harness.config.reasoning_effort))

    def inherited_environment(self) -> dict[str, str]:
        environment = super().inherited_environment()
        _ensure_local_proxy_bypass(environment)
        return environment

    def harness_environment(self, api_base: str) -> dict[str, str]:
        home = self.home
        return dict(
            # Preserve the task image's HOME. Benchmark environments commonly
            # install browser/tool caches under /root and verifier scripts use
            # $HOME to find them. OpenCode state remains isolated through the
            # explicit XDG and OPENCODE_CONFIG paths below.
            OPENAI_BASE_URL=api_base,
            XDG_CONFIG_HOME=home + "/config",
            XDG_DATA_HOME=home + "/data",
            XDG_STATE_HOME=home + "/state",
            OPENCODE_CONFIG=STATE + "/opencode.json",
            OPENCODE_DISABLE_PROJECT_CONFIG="1", OPENCODE_DISABLE_MODELS_FETCH="1",
            OPENCODE_DISABLE_AUTOUPDATE="1", OPENCODE_DISABLE_PRUNE="1",
            OPENCODE_DISABLE_AUTOCOMPACT="1", OPENCODE_DISABLE_FFF="1",
            OPENCODE_DISABLE_DEFAULT_PLUGINS="1",
            ROLLER_OPENCODE_JOURNAL=STATE + "/trace.jsonl",
        )

    def setup_script(self) -> str:
        config_dir = self.home + "/config/opencode"
        return (
            f" && mkdir -p {config_dir}"
            " && test -d /opt/roller-opencode-deps/node_modules"
            f" && cp /opt/roller-opencode-deps/package*.json {config_dir}/"
            f" && ln -s /opt/roller-opencode-deps/node_modules {config_dir}/node_modules"
        )

    def system_prompt(self) -> str:
        return _system_prompt(self.request, self.harness.config.workspace_container_path)

    def turn_options(self, timeout: float) -> dict[str, Any]:
        # Context is present as regular workspace files for every role; OpenCode's
        # implicit host/profile discovery is disabled in the bridge.
        config = self.harness.config
        return {
            "max_steps": config.max_steps, "context_length": config.context_length,
            "output_tokens": config.output_tokens, "vision": config.vision,
        }

    def reset(self) -> None:
        """Forget conversation state without touching the task workspace."""

        if not self.container_started or self._result is not None:
            raise RuntimeError("OpenCode session is not active")
        self.checked(
            ["exec", self.container, "rm", "-f", STATE + "/session.json"],
            "OpenCode session reset",
        )

    def convert_trace(self, trace: Path, destination: Path) -> dict[str, Any]:
        return convert_opencode_trace(trace, destination, keep_thinking=self.harness.config.keep_thinking)

    def extract_usage(self, trace: Path) -> dict[str, int | float]:
        return extract_opencode_usage(trace)

    def version_fields(self) -> dict[str, Any]:
        return {"opencode_version": self.version}

    def settings(self) -> dict[str, Any]:
        config = self.harness.config
        return {
            "max_steps": config.max_steps, "context_length": config.context_length,
            "output_tokens": config.output_tokens, "vision": config.vision,
            "tools": [*OPENCODE_TOOLS, *(["task"] if config.capture_calls else [])],
            **({"capture_calls": True, "subagent_profile_policy": "explore-tool-restrictions/v1"} if config.capture_calls else {}),
        }

    def result_metadata(self) -> dict[str, Any]:
        return {"warnings": self.warnings, "trajectory_conversion": self.trajectory_conversion}


class OpenCodeHarness(BridgeHarness):
    """Run the pinned OpenCode CLI in an isolated task Docker container."""

    name = "opencode"
    session_class = OpenCodeSession
