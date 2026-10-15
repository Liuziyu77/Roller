from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from roller.harnesses.bridge import BridgeHarness, BridgeSession, positive_int, validate_bridge_config
from roller.harnesses.container import PROXY_ENV_NAMES as COMMON_ENV_NAMES
from roller.hermes_actf import convert_hermes_trace, extract_hermes_usage
from roller.thinking import normalize_thinking, resolve_thinking
from roller.usage import ZERO_USAGE

HERMES_REVISION = "d15ed4445207dda418b984e8bda0f68f48b8c6f3"
DEFAULT_HERMES_IMAGE = "roller-hermes:d15ed4445207"
RUNNER_PATH = Path(__file__).resolve().parents[1] / "assets" / "hermes" / "runner.py"
STATE = "/roller/hermes-state"
RUNNER = "/roller/hermes_runner.py"


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
    vision: bool = True
    capture_calls: bool = False
    capture_debug: str = "on-error"
    wire_trace_mode: str = "actf"
    keep_thinking: bool = True
    keep_container_on_error: bool = False
    docker_binary: str = "docker"

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasoning_effort", hermes_effort(self.reasoning_effort))
        validate_bridge_config(self, "Hermes")
        if not positive_int(self.max_iterations):
            raise ValueError("Hermes max iterations must be a positive integer")
        if self.context_length is not None and not (positive_int(self.context_length) and self.context_length >= 64000):
            raise ValueError("Hermes context length must be at least 64000 tokens")


class HermesSession(BridgeSession):
    label = "Hermes"
    state_dir = STATE
    runner = RUNNER
    runner_source = RUNNER_PATH
    raw_trace_schema = "roller.hermes-events/v1"
    python = "python"
    version_check = "revision and hook check"

    def effort(self) -> str | None:
        return hermes_effort(resolve_thinking(self.request.task.thinking, self.harness.config.reasoning_effort))

    def harness_environment(self, api_base: str) -> dict[str, str]:
        return dict(
            OPENAI_BASE_URL=api_base, HERMES_HOME=self.home, HERMES_DISABLE_LAZY_INSTALLS="1",
            TERMINAL_ENV="local", TERMINAL_CWD=self.harness.config.workspace_container_path,
        )

    def turn_options(self, timeout: float) -> dict[str, Any]:
        # Context is present as regular workspace files for every role; Hermes'
        # implicit host/profile discovery is disabled in the bridge.
        config = self.harness.config
        return {"max_iterations": config.max_iterations, "context_length": config.context_length, "vision": config.vision}

    def convert_trace(self, trace: Path, destination: Path) -> dict[str, Any]:
        return convert_hermes_trace(trace, destination, keep_thinking=self.harness.config.keep_thinking)

    def extract_usage(self, trace: Path) -> dict[str, int | float]:
        return extract_hermes_usage(trace)

    def version_fields(self) -> dict[str, Any]:
        return {"hermes_revision": HERMES_REVISION, "hermes_version": self.version}

    def settings(self) -> dict[str, Any]:
        config = self.harness.config
        toolsets = ["terminal", "file", *(["delegation"] if config.capture_calls else []), *(["vision"] if config.vision else [])]
        return {"max_iterations": config.max_iterations, "context_length": config.context_length, "vision": config.vision, "toolsets": toolsets}

    def collect_native_evidence(self, usage: dict[str, int | float]) -> None:
        assert self.capture is not None
        self.capture.copy_native(self.harness.runtime, self.container, STATE + "/capture-native/.", "children")
        children = self.capture.recorder.directory / "native" / "children"
        if not children.is_dir():
            return
        for child in children.glob("*.jsonl"):
            child_trace = children.parent / ("child-" + child.name)
            child.rename(child_trace)
            # Count child model responses once, independently of the root
            # ACTF, before compact capture removes raw logs.
            try:
                child_usage = extract_hermes_usage(child_trace)
            except Exception as exc:
                self.capture.errors.append("Hermes child usage extraction failed: " + self.redact(str(exc)))
            else:
                for key in ZERO_USAGE:
                    usage[key] += child_usage[key]
        children.rmdir()


class HermesHarness(BridgeHarness):
    """Run the pinned Hermes Python agent with local tools inside task Docker."""

    name = "hermes"
    session_class = HermesSession
