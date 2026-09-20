"""Score aggregation for SWE-Bench Pro samples."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from roller.benches.report import mean_aggregate


def aggregate(samples: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return mean_aggregate(samples)
