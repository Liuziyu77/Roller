from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any

from roller.harnesses.bridge import BridgeHarness, BridgeSession, validate_bridge_config, validate_step_limits
from roller.harnesses.container import PROXY_ENV_NAMES as COMMON_ENV_NAMES
from roller.pi_actf import convert_pi_trace, extract_pi_usage
from roller.thinking import normalize_thinking, resolve_thinking

logger = logging.getLogger(__name__)
PI_VERSION = "0.85.1"
DEFAULT_PI_IMAGE = "roller-pi:0.85.1"
RUNNER_PATH = Path(__file__).resolve().parents[1] / "assets" / "pi" / "runner.py"
STATE = "/roller/pi-state"
RUNNER = "/roller/pi_runner.py"
SDK_RUNNER = "/roller/pi_sdk.mjs"
PI_TOOLS = ("read", "bash", "edit", "write", "grep", "find", "ls")


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
        validate_bridge_config(self, "Pi")
        validate_step_limits(self, "Pi")


class PiSession(BridgeSession):
    label = "Pi"
    state_dir = STATE
    runner = RUNNER
    runner_source = RUNNER_PATH
    raw_trace_schema = "roller.pi-events/v1"
    api_key_name = "PI_API_KEY"
    assets = (("runner.mjs", SDK_RUNNER, "SDK runner copy"),)
    redact_trace = True

    def effort(self) -> str | None:
        return pi_effort(resolve_thinking(self.request.task.thinking, self.harness.config.reasoning_effort))

    def harness_environment(self, api_base: str) -> dict[str, str]:
        return dict(PI_BASE_URL=api_base, HOME=self.home, PI_CODING_AGENT_DIR=self.home + "/agent", PI_OFFLINE="1")

    def turn_options(self, timeout: float) -> dict[str, Any]:
        # Pass explicit Context paths while keeping Pi resource discovery isolated.
        config = self.harness.config
        return {
            "turn": self.turns, "max_steps": config.max_steps,
            "context_length": config.context_length, "output_tokens": config.output_tokens,
            "tools": list(PI_TOOLS),
            "context_paths": [item.target for item in self.request.task.context_files if item.role != "skill"],
        }

    def convert_trace(self, trace: Path, destination: Path) -> dict[str, Any]:
        return convert_pi_trace(trace, destination, keep_thinking=self.harness.config.keep_thinking)

    def extract_usage(self, trace: Path) -> dict[str, int | float]:
        return extract_pi_usage(trace)

    def version_fields(self) -> dict[str, Any]:
        return {"pi_version": self.version, "api_mode": "openai-completions"}

    def settings(self) -> dict[str, Any]:
        config = self.harness.config
        return {"max_steps": config.max_steps, "context_length": config.context_length, "output_tokens": config.output_tokens, "tools": list(PI_TOOLS)}

    def collect_diagnostics(self) -> None:
        # Startup/import failures may precede the native journal.
        try:
            log = self.output_dir / "rollout" / f"pi.runtime.attempt-{self.attempt}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            self.harness.runtime.run(["cp", f"{self.container}:{STATE}/runtime.log", str(log)], timeout=30)
            if log.is_file():
                log.write_text(self.redact(log.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
        except Exception:
            logger.warning("Pi runtime log collection failed", exc_info=True)


class PiHarness(BridgeHarness):
    """Run the official Pi AgentSession and tools in a task Docker container."""

    name = "pi"
    session_class = PiSession
