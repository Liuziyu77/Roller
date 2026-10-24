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
_PATH_SEGMENT = re.compile(r"""\[(?:"([^"]*)"|'([^']*)'|([^\]]*))\]|([^.\[\]]+)""")


def parse_config_assignment(text: str) -> ConfigAssignment:
    """Split ``KEY=VALUE`` at the first ``=``; the value is kept verbatim."""

    key, separator, value = text.partition("=")
    key = key.strip()
    if not separator or not key:
        raise ValueError(f"expected KEY=VALUE, got {text!r}")
    config_path(key)
    return key, value


def config_path(key: str) -> tuple[str, ...]:
    """Return the segments of a dotted or bracketed configuration path.

    Rejects keys that are empty, start with ``-``, or contain whitespace or a
    backslash. OpenClaw unescapes backslashes, so ``g\\ateway.port`` would reach
    a managed path without matching it here.
    """

    path = tuple(
        next(group for group in match.groups() if group is not None)
        for match in _PATH_SEGMENT.finditer(key)
    )
    if (
        not path
        or key.startswith("-")
        or "\\" in key
        or any(character.isspace() for character in key)
    ):
        raise ValueError(f"invalid configuration key: {key!r}")
    return path


def _overlaps(path: tuple[str, ...], other: tuple[str, ...]) -> bool:
    shared = min(len(path), len(other))
    return path[:shared] == other[:shared]


def validate_config_assignments(
    assignments: Iterable[ConfigAssignment],
    managed: Mapping[str, str],
    *,
    flag: str,
) -> tuple[ConfigAssignment, ...]:
    """Reject keys that overlap each other or a Roller-managed path.

    ``managed`` maps each path Roller writes to a hint naming the Roller option
    that controls it. Keys may not overlap each other either: the Harness
    applies them in order, which the recorded ``{key: value}`` does not keep.
    """

    validated = tuple(assignments)
    for index, (key, _) in enumerate(validated):
        path = config_path(key)
        for earlier, _ in validated[:index]:
            if _overlaps(path, config_path(earlier)):
                raise ValueError(
                    f"{flag} cannot set {key!r}: it overlaps {earlier!r}, set before it"
                )
        for managed_key, hint in managed.items():
            if _overlaps(path, config_path(managed_key)):
                raise ValueError(
                    f"{flag} cannot set {key!r}: it overlaps {managed_key!r}, "
                    f"which Roller sets itself ({hint})"
                )
    return validated


def recorded_config_assignments(
    assignments: Iterable[ConfigAssignment],
) -> dict[str, str]:
    """``{key: value}`` for run metadata, with credential-like leaves redacted."""

    recorded: dict[str, str] = {}
    for key, value in assignments:
        leaf = config_path(key)[-1]
        recorded[key] = redact_sensitive_values({leaf: value})[leaf]
    return recorded
