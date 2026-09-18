"""Benchmark adapters: turn upstream benches into Roller tasks, then score them.

Adding a bench means writing one :class:`BenchAdapter` and registering it in
``BENCHES`` below. Nothing else in Roller needs to change: the adapter emits
ordinary ``roller.task/v1`` rows whose optional ``bench`` block carries the
task's scorer, and the collect path already ignores row keys it does not know.
"""

from __future__ import annotations

from roller.benches.base import (
    BENCH_SCHEMA,
    BenchAdapter,
    BenchBinding,
    BenchFormatError,
    GradeSpec,
    binding_from_task_row,
)
from roller.benches.report import (
    SCORE_FILE,
    format_report,
    mean_aggregate,
    read_scores,
    sample_score,
)
from roller.benches.skillsbench import SkillsBenchAdapter
from roller.benches.wildclaw import WildClawBenchAdapter

BENCHES: dict[str, BenchAdapter] = {
    adapter.name: adapter
    for adapter in (
        SkillsBenchAdapter(),
        WildClawBenchAdapter(),
    )
}


def get_bench(name: str) -> BenchAdapter:
    try:
        return BENCHES[name]
    except KeyError:
        known = ", ".join(sorted(BENCHES)) or "none registered"
        raise KeyError(f"unknown bench {name!r}; available: {known}") from None


def list_benches() -> list[BenchAdapter]:
    return [BENCHES[name] for name in sorted(BENCHES)]


__all__ = [
    "BENCHES",
    "BENCH_SCHEMA",
    "SCORE_FILE",
    "BenchAdapter",
    "BenchBinding",
    "BenchFormatError",
    "GradeSpec",
    "SkillsBenchAdapter",
    "WildClawBenchAdapter",
    "binding_from_task_row",
    "format_report",
    "get_bench",
    "list_benches",
    "mean_aggregate",
    "read_scores",
    "sample_score",
]
