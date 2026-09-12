"""Dependency-free Harness capabilities shared by parsing and artifact checks."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HarnessCapabilities:
    label: str
    cache_trace: bool = False
    external_thinking: bool = False


HARNESSES = {
    "openclaw": HarnessCapabilities("OpenClaw", cache_trace=True, external_thinking=True),
    "codex": HarnessCapabilities("Codex", external_thinking=True),
    "claude-code": HarnessCapabilities("Claude Code"),
    "hermes": HarnessCapabilities("Hermes"),
    "opencode": HarnessCapabilities("OpenCode"),
    "deepseek": HarnessCapabilities("DeepSeek Harness"),
    "pi": HarnessCapabilities("Pi Agent"),
}
HARNESS_NAMES = tuple(HARNESSES)


def requires_cache_trace(harness: object) -> bool:
    """Unknown/legacy metadata retains the collector's strict capture policy."""
    capability = HARNESSES.get(harness) if isinstance(harness, str) else None
    return capability.cache_trace if capability is not None else True
