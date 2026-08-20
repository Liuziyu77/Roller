from __future__ import annotations

from typing import Final


# These are OpenClaw's user-facing logical levels, not the raw values accepted
# by every provider API. OpenClaw's model profile owns the provider-specific
# validation and mapping (for example, GPT maps ``off`` to API effort ``none``).
THINKING_LEVELS: Final[tuple[str, ...]] = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "adaptive",
    "max",
    "ultra",
)
THINKING_ALIASES: Final[dict[str, str]] = {"none": "off"}
THINKING_INPUT_VALUES: Final[tuple[str, ...]] = (
    *THINKING_LEVELS,
    *THINKING_ALIASES,
)


def normalize_thinking(value: str | None, *, field: str = "thinking") -> str | None:
    """Return one canonical OpenClaw thinking level.

    ``none`` is accepted as a GPT-friendly input alias, but OpenClaw's logical
    spelling is ``off``. Provider/model compatibility remains OpenClaw's job.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip().lower()
    normalized = THINKING_ALIASES.get(normalized, normalized)
    if normalized not in THINKING_LEVELS:
        accepted = "|".join(THINKING_INPUT_VALUES)
        raise ValueError(f"{field} must be one of {accepted}: {value!r}")
    return normalized


def resolve_thinking(task_value: str | None, default_value: str | None) -> str | None:
    """Resolve a task override without allowing malformed values to fall back."""

    selected = task_value if task_value is not None else default_value
    return normalize_thinking(selected)
