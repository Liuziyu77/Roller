"""Shared OpenClaw token/cost parsing for delivery matching and accounting."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any


TOKEN_FIELDS = ("input", "output", "cacheRead", "cacheWrite", "totalTokens")
TokenCounts = tuple[int | float, ...]


def _valid_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
        and (not isinstance(value, float) or isfinite(value))
    )


@dataclass(frozen=True)
class OpenClawUsage:
    counts: TokenCounts
    cost_usd: float
    # Malformed counters may be accounted field by field, but must never
    # become evidence that two requests are the same.
    matchable: bool

    @classmethod
    def parse(cls, raw: Any) -> OpenClawUsage | None:
        if not isinstance(raw, dict):
            return None
        components = [raw.get(key, 0) for key in TOKEN_FIELDS[:-1]]
        matchable = all(_valid_number(value) for value in components)
        components = [value if _valid_number(value) else 0 for value in components]
        total = raw.get("totalTokens")
        # Null/absent totals fall back to the component sum, including cache.
        # Keep explicit totals: CLI aggregates can carry the last call's
        # context total instead. Delivery matching handles that distinction.
        if total is None:
            total = sum(components)
        elif not _valid_number(total):
            matchable = False
            total = sum(components)
        cost = raw.get("cost")
        cost = cost.get("total") if isinstance(cost, dict) else None
        return cls(
            counts=(*components, total),
            cost_usd=float(cost) if _valid_number(cost) else 0.0,
            matchable=matchable,
        )
