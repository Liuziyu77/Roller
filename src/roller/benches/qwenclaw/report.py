"""QwenClawBench sample aggregation."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from roller.benches.report import mean_aggregate, sample_score


def aggregate_samples(samples: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(samples)
    report = mean_aggregate(rows)
    grouped: dict[str, list[float]] = {}
    for row in rows:
        metadata = row.get("bench_metadata")
        upstream_id = (
            metadata.get("upstream_task_id")
            if isinstance(metadata, dict)
            else row.get("task_id")
        )
        scores = row.get("bench_scores")
        value = (
            sample_score(scores, row.get("bench_score_key") or "overall_score")
            if isinstance(scores, dict)
            else None
        )
        if isinstance(upstream_id, str):
            grouped.setdefault(upstream_id, []).append(
                value if value is not None else 0.0
            )
    task_means = {
        task_id: sum(values) / len(values) for task_id, values in grouped.items()
    }
    perfect = sum(value >= 1.0 - 1e-9 for value in task_means.values())
    report["average"] = (
        round(sum(task_means.values()) / len(task_means), 6)
        if task_means
        else None
    )
    report["upstream_task_count"] = len(task_means)
    report["strict_accuracy"] = (
        round(perfect / len(task_means), 6) if task_means else None
    )
    report["perfect_task_count"] = perfect
    report["runs_per_task"] = {
        task_id: len(values) for task_id, values in sorted(grouped.items())
    }
    return report
