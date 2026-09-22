from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from roller.thinking import THINKING_LEVELS
from roller.harness_registry import HARNESS_NAMES


SCHEMA_VERSION = "roller.thinking-capabilities/v1"
PROFILE_HARNESSES = frozenset(HARNESS_NAMES)


def _endpoint(value: str | None) -> str | None:
    return value.rstrip("/") if value else None


@dataclass(frozen=True)
class ThinkingCapability:
    harness: str
    model: str
    supported_efforts: frozenset[str]
    api_base: str | None = None


@dataclass(frozen=True)
class ThinkingCapabilities:
    profiles: tuple[ThinkingCapability, ...]

    def match(
        self, *, harness: str, model: str, api_base: str | None
    ) -> ThinkingCapability | None:
        endpoint = _endpoint(api_base)
        matches = [
            profile
            for profile in self.profiles
            if profile.harness == harness
            and profile.model == model
            and (
                profile.api_base is None
                or _endpoint(profile.api_base) == endpoint
            )
        ]
        if not matches:
            return None
        return max(matches, key=lambda profile: profile.api_base is not None)

    def validate(
        self,
        *,
        harness: str,
        model: str,
        api_base: str | None,
        effort: str | None,
    ) -> ThinkingCapability | None:
        profile = self.match(harness=harness, model=model, api_base=api_base)
        if profile is None or effort is None:
            return profile
        if effort not in profile.supported_efforts:
            supported = "|".join(
                level
                for level in THINKING_LEVELS
                if level in profile.supported_efforts
            )
            endpoint = f" at {_endpoint(api_base)}" if api_base else ""
            raise ValueError(
                f"thinking effort {effort!r} is not declared supported for "
                f"{harness} model {model!r}{endpoint}; supported efforts: "
                f"{supported or '(none)'}"
            )
        return profile


def load_thinking_capabilities(path: Path) -> ThinkingCapabilities:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid thinking capabilities JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("thinking capabilities must be a JSON object")
    unknown = set(payload) - {"schema_version", "profiles"}
    if unknown:
        raise ValueError(f"thinking capabilities has unknown fields: {sorted(unknown)}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"thinking capabilities schema_version must be {SCHEMA_VERSION!r}"
        )
    rows = payload.get("profiles")
    if not isinstance(rows, list):
        raise ValueError("thinking capabilities profiles must be a JSON array")

    profiles: list[ThinkingCapability] = []
    keys: set[tuple[str, str, str | None]] = set()
    for index, row in enumerate(rows):
        label = f"thinking capabilities profiles[{index}]"
        if not isinstance(row, dict):
            raise ValueError(f"{label} must be a JSON object")
        unknown = set(row) - {
            "harness",
            "model",
            "api_base",
            "supported_efforts",
        }
        if unknown:
            raise ValueError(f"{label} has unknown fields: {sorted(unknown)}")
        harness = row.get("harness")
        model = row.get("model")
        api_base = row.get("api_base")
        efforts = row.get("supported_efforts")
        if not isinstance(harness, str) or not harness.strip():
            raise ValueError(f"{label}.harness must be a non-empty string")
        if harness not in PROFILE_HARNESSES:
            raise ValueError(f"{label}.harness has no capability-profile integration: {harness!r}")
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"{label}.model must be a non-empty string")
        if model != model.strip():
            raise ValueError(f"{label}.model must not have surrounding whitespace")
        if api_base is not None and (
            not isinstance(api_base, str) or not api_base.strip()
        ):
            raise ValueError(f"{label}.api_base must be a non-empty string")
        if api_base is not None and api_base != api_base.strip():
            raise ValueError(f"{label}.api_base must not have surrounding whitespace")
        if not isinstance(efforts, list) or any(
            not isinstance(effort, str) or effort not in THINKING_LEVELS
            for effort in efforts
        ):
            raise ValueError(
                f"{label}.supported_efforts must contain only Roller thinking levels"
            )
        if len(efforts) != len(set(efforts)):
            raise ValueError(f"{label}.supported_efforts contains duplicates")
        key = (harness.strip(), model.strip(), _endpoint(api_base))
        if key in keys:
            raise ValueError(f"duplicate thinking capability profile: {key}")
        keys.add(key)
        profiles.append(
            ThinkingCapability(
                harness=harness.strip(),
                model=model.strip(),
                api_base=_endpoint(api_base),
                supported_efforts=frozenset(efforts),
            )
        )
    return ThinkingCapabilities(tuple(profiles))
