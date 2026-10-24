"""``KEY=VALUE`` settings passed through to a Harness's own configuration.

Roller writes part of each Harness's configuration itself (ports, tokens, the
model provider, ...). A pass-through setting may add to that configuration but
never overlap it: a parent path would replace Roller's values and a child path
would edit them, both behind the matching Roller flag's back.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping

from roller.wire_trace import redact_sensitive_values

ConfigAssignment = tuple[str, str]

# ``a.b``, ``a["b"]`` and ``a[b]`` all name the same segments.
_PATH_SEGMENT = re.compile(r"""\[\s*(?:"([^"]*)"|'([^']*)'|([^\]]*))\s*\]|([^.\[\]]+)""")


def parse_config_assignment(text: str) -> ConfigAssignment:
    """Split ``KEY=VALUE`` at the first ``=``; the value is kept verbatim."""

    key, separator, value = text.partition("=")
    key = key.strip()
    if not separator or not key:
        raise ValueError(f"expected KEY=VALUE, got {text!r}")
    if key.startswith("-") or any(character.isspace() for character in key):
        raise ValueError(f"invalid configuration key: {key!r}")
    if not config_path(key):
        raise ValueError(f"invalid configuration key: {key!r}")
    return key, value


def config_path(key: str) -> tuple[str, ...]:
    """Return the segments of a dotted or bracketed configuration path."""

    return tuple(
        next(group for group in match.groups() if group is not None)
        for match in _PATH_SEGMENT.finditer(key)
    )


def validate_config_assignments(
    assignments: Iterable[ConfigAssignment],
    managed: Mapping[str, str],
    *,
    flag: str,
) -> tuple[ConfigAssignment, ...]:
    """Reject duplicate keys and keys that overlap a Roller-managed path.

    ``managed`` maps each path Roller writes to a hint naming the Roller option
    that controls it.
    """

    validated: list[ConfigAssignment] = []
    seen: dict[tuple[str, ...], str] = {}
    for key, value in assignments:
        path = config_path(key)
        if not path:
            raise ValueError(f"{flag}: invalid configuration key: {key!r}")
        if path in seen:
            raise ValueError(f"{flag} sets {key!r} more than once")
        seen[path] = key
        for managed_key, hint in managed.items():
            managed_path = config_path(managed_key)
            shared = min(len(path), len(managed_path))
            if path[:shared] == managed_path[:shared]:
                raise ValueError(
                    f"{flag} cannot set {key!r}: it overlaps {managed_key!r}, "
                    f"which Roller sets itself ({hint})"
                )
        validated.append((key, value))
    return tuple(validated)


def recorded_config_assignments(
    assignments: Iterable[ConfigAssignment],
) -> dict[str, str]:
    """``{key: value}`` for run metadata, with credential-like leaves redacted."""

    recorded: dict[str, str] = {}
    for key, value in assignments:
        leaf = config_path(key)[-1]
        recorded[key] = redact_sensitive_values({leaf: value})[leaf]
    return recorded
