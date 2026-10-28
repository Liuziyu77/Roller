"""Dependency-free Harness capabilities shared by parsing and artifact checks."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HarnessCapabilities:
    label: str
    cache_trace: bool = False
    capture_reader: str = ""
    capture_reader_kwargs: tuple[tuple[str, str], ...] = ()
    compaction_mode: str = "request"
    allows_raw_only_actf: bool = False


HARNESSES = {
    "openclaw": HarnessCapabilities("OpenClaw", cache_trace=True,
        capture_reader="roller.openclaw_capture:read_openclaw_native",
        compaction_mode="openclaw", allows_raw_only_actf=True),
    "codex": HarnessCapabilities("Codex", capture_reader="roller.codex_capture:read_codex_native",
        compaction_mode="codex"),
    "claude-code": HarnessCapabilities("Claude Code", capture_reader="roller.claude_capture:read_claude_native",
        compaction_mode="claude"),
    "hermes": HarnessCapabilities("Hermes", capture_reader="roller.bridge_capture:read_bridge_native",
        capture_reader_kwargs=(("harness", "hermes"),)),
    "opencode": HarnessCapabilities("OpenCode", capture_reader="roller.opencode_capture:read_opencode_native",
        compaction_mode="opencode", allows_raw_only_actf=True),
    "deepseek": HarnessCapabilities("DeepSeek Harness", capture_reader="roller.bridge_capture:read_bridge_native",
        capture_reader_kwargs=(("harness", "deepseek"),)),
    "pi": HarnessCapabilities("Pi Agent", capture_reader="roller.bridge_capture:read_bridge_native",
        capture_reader_kwargs=(("harness", "pi"),), compaction_mode="pi"),
}
HARNESS_NAMES = tuple(HARNESSES)


def requires_cache_trace(harness: object) -> bool:
    """Unknown/legacy metadata retains the collector's strict capture policy."""
    capability = HARNESSES.get(harness) if isinstance(harness, str) else None
    return capability.cache_trace if capability is not None else True


def compaction_mode(harness: object) -> str:
    capability = HARNESSES.get(harness) if isinstance(harness, str) else None
    return capability.compaction_mode if capability is not None else "request"


def allows_raw_only_actf(harness: object) -> bool:
    capability = HARNESSES.get(harness) if isinstance(harness, str) else None
    return capability.allows_raw_only_actf if capability is not None else False


def read_native_capture(harness: str, directory: Path) -> Any:
    """Load a harness's native evidence reader without importing adapters at startup."""
    capability = HARNESSES[harness]
    module_name, reader_name = capability.capture_reader.split(":", 1)
    reader = getattr(import_module(module_name), reader_name)
    return reader(directory, **dict(capability.capture_reader_kwargs))
