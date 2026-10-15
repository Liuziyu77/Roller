from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from roller.deepseek_actf import convert_deepseek_trace, extract_deepseek_usage
from roller.harnesses.bridge import BridgeHarness, BridgeSession, validate_bridge_config, validate_step_limits
from roller.harnesses.container import PROXY_ENV_NAMES as COMMON_ENV_NAMES
from roller.thinking import normalize_thinking, resolve_thinking

DEEPSEEK_VERSION = "0.1.5rc1"
DEFAULT_DEEPSEEK_IMAGE = "roller-deepseek:0.1.5rc1"
RUNNER_PATH = Path(__file__).resolve().parents[1] / "assets" / "deepseek" / "runner.py"
STATE = "/roller/deepseek-state"
RUNNER = "/roller/deepseek_runner.py"
PLUGIN = "/roller/deepseek_journal.mjs"
DEEPSEEK_TOOLS = ("bash",)


def deepseek_effort(value: str | None) -> str | None:
    value = normalize_thinking(value, field="DeepSeek reasoning effort")
    if value not in {None, "off", "low", "high", "max"}:
        raise ValueError("DeepSeek reasoning effort must be off|low|high|max; provider support is required")
    return value


@dataclass(frozen=True)
class DeepSeekConfig:
    docker_image: str = DEFAULT_DEEPSEEK_IMAGE
    container_user: str | None = "0"
    network: str | None = "host"
    workspace_container_path: str = "/roller/workspace"
    api_base: str | None = None
    api_key_env: str = "DEEPSEEK_API_KEY"
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
        object.__setattr__(self, "reasoning_effort", deepseek_effort(self.reasoning_effort))
        validate_bridge_config(self, "DeepSeek")
        validate_step_limits(self, "DeepSeek")


class DeepSeekSession(BridgeSession):
    label = "DeepSeek"
    state_dir = STATE
    runner = RUNNER
    runner_source = RUNNER_PATH
    raw_trace_schema = "roller.deepseek-events/v1"
    api_key_name = "DEEPSEEK_API_KEY"
    default_api_base = "https://api.deepseek.com"
    assets = (
        ("journal.mjs", PLUGIN, "journal plugin copy"),
        ("roller.patch.yml", STATE + "/roller.patch.yml", "profile patch copy"),
    )
    redact_trace = True

    def effort(self) -> str | None:
        return deepseek_effort(resolve_thinking(self.request.task.thinking, self.harness.config.reasoning_effort))

    def harness_environment(self, api_base: str) -> dict[str, str]:
        return dict(
            DEEPSEEK_BASE_URL=api_base, HOME=self.home, DSH_HOME=self.home,
            DSH_RUNTIME_MODE="exe", ROLLER_DEEPSEEK_JOURNAL=STATE + "/trace.jsonl",
        )

    def turn_options(self, timeout: float) -> dict[str, Any]:
        # The minimal profile uses explicit context files and an isolated DSH_HOME.
        config = self.harness.config
        return {
            "turn": self.turns, "max_steps": config.max_steps,
            "context_length": config.context_length, "output_tokens": config.output_tokens,
            "tool_env_names": sorted(set((*config.pass_env, *self.request.task.env_names))),
        }

    def convert_trace(self, trace: Path, destination: Path) -> dict[str, Any]:
        return convert_deepseek_trace(trace, destination, keep_thinking=self.harness.config.keep_thinking)

    def extract_usage(self, trace: Path) -> dict[str, int | float]:
        return extract_deepseek_usage(trace)

    def version_fields(self) -> dict[str, Any]:
        return {"deepseek_version": self.version, "profile": "sdk-minimal"}

    def settings(self) -> dict[str, Any]:
        config = self.harness.config
        return {"max_steps": config.max_steps, "context_length": config.context_length, "output_tokens": config.output_tokens, "tools": list(DEEPSEEK_TOOLS)}


class DeepSeekHarness(BridgeHarness):
    """Run the official DeepSeek SDK minimal profile in a task Docker container."""

    name = "deepseek"
    session_class = DeepSeekSession
