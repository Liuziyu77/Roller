"""Dependency-free Harness capabilities shared by parsing and artifact checks."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HarnessCapabilities:
    label: str
    cache_trace: bool = False
    subagent_tool: str | None = None


HARNESSES = {
    "openclaw": HarnessCapabilities("OpenClaw", cache_trace=True, subagent_tool="sessions_spawn"),
    "codex": HarnessCapabilities("Codex", subagent_tool="spawn_agent"),
    "claude-code": HarnessCapabilities("Claude Code", subagent_tool="Agent"),
    "hermes": HarnessCapabilities("Hermes", subagent_tool="delegate_task"),
    "opencode": HarnessCapabilities("OpenCode", subagent_tool="task"),
    "deepseek": HarnessCapabilities("DeepSeek Harness"),
    "pi": HarnessCapabilities("Pi Agent"),
}
HARNESS_NAMES = tuple(HARNESSES)


def requires_cache_trace(harness: object) -> bool:
    """Unknown/legacy metadata retains the collector's strict capture policy."""
    capability = HARNESSES.get(harness) if isinstance(harness, str) else None
    return capability.cache_trace if capability is not None else True
