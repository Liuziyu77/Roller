from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

SCORE_FILE = "score.json"
REPORT_SCHEMA = "roller.bench_report/v1"


def _numeric(scores: dict[str, Any]) -> dict[str, float]:
    return {
        key: float(value)
        for key, value in scores.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def sample_score(scores: dict[str, Any], score_key: str = "overall_score") -> float | None:
    """Reduce one grader payload to a single number.

    Follows WildClawBench's rule: prefer the declared aggregate key, otherwise
    average whatever numeric fields the grader returned.
    """

    numeric = _numeric(scores)
    if not numeric:
        return None
    if score_key in numeric:
        return numeric[score_key]
    return sum(numeric.values()) / len(numeric)


def read_scores(sample_dir: Path) -> dict[str, Any] | None:
    path = sample_dir / SCORE_FILE
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def mean_aggregate(samples: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Average scored samples over *all* samples, counting failures as zero.

    An agent that crashed or timed out earned no score; dropping it from the
    denominator would silently reward failure. Unscored samples are reported
    separately so a broken grader is not mistaken for a weak model.
    """

    rows = list(samples)
    per_task: list[dict[str, Any]] = []
    per_category: dict[str, list[float]] = {}
    total = 0.0
    scored = 0
    for row in rows:
        scores = row.get("bench_scores")
        score_key = row.get("bench_score_key") or "overall_score"
        grader_error = row.get("bench_error")
        if not grader_error and isinstance(scores, dict):
            grader_error = scores.get("error")
        # A synthetic zero returned alongside a grader/infrastructure error is
        # not a model score. Keep it unscored so broken grading cannot depress
        # benchmark results while still surfacing the failure in the report.
        value = (
            sample_score(scores, score_key)
            if isinstance(scores, dict) and not grader_error
            else None
        )
        category = row.get("bench_category")
        if value is not None:
            total += value
            scored += 1
            if isinstance(category, str) and category:
                per_category.setdefault(category, []).append(value)
        per_task.append(
            {
                "task_id": row.get("task_id"),
                "model": row.get("model"),
                "category": category,
                "status": row.get("status"),
                "score": value,
                "error": grader_error,
            }
        )
    task_count = len(rows)
    return {
        "schema_version": REPORT_SCHEMA,
        "task_count": task_count,
        "scored_count": scored,
        "unscored_count": task_count - scored,
        "average": round(total / task_count, 6) if task_count else None,
        "scored_average": round(total / scored, 6) if scored else None,
        "categories": {
            name: {
                "task_count": len(values),
                "average": round(sum(values) / len(values), 6),
            }
            for name, values in sorted(per_category.items())
        },
        "tasks": per_task,
    }


def format_report(bench_name: str, report: dict[str, Any]) -> str:
    lines = [
        "",
        "=" * 62,
        f"  Bench report — {bench_name}",
        "=" * 62,
    ]
    average = report.get("average")
    if average is None:
        lines.append("  no samples")
    else:
        filled = max(0, min(10, int(round(average * 10))))
        bar = "█" * filled + "░" * (10 - filled)
        lines.append(
            f"  scored {report['scored_count']}/{report['task_count']}"
            f"  (unscored {report['unscored_count']})"
        )
        pass_rate = report.get("pass_rate")
        if isinstance(pass_rate, (int, float)):
            pass_bar_count = max(0, min(10, int(round(pass_rate * 10))))
            pass_bar = "█" * pass_bar_count + "░" * (10 - pass_bar_count)
            lines.append(f"  pass rate   {pass_bar} {pass_rate:.4f}")
            lines.append(f"  mean reward {bar} {average:.4f}")
        else:
            lines.append(f"  average  {bar} {average:.4f}")
        scored_average = report.get("scored_average")
        if (
            scored_average is not None
            and report["unscored_count"]
            and not isinstance(pass_rate, (int, float))
        ):
            lines.append(f"  average over scored samples only: {scored_average:.4f}")
    categories = report.get("categories") or {}
    if categories:
        lines.append("")
        for name, row in categories.items():
            if isinstance(row.get("pass_rate"), (int, float)):
                lines.append(
                    f"    pass {row['pass_rate']:.4f}  reward {row['average']:.4f}"
                    f"  {name}  ({row['task_count']} trials)"
                )
            else:
                lines.append(
                    f"    {row['average']:.4f}  {name}  ({row['task_count']} tasks)"
                )
    failures = [
        row
        for row in report.get("tasks", [])
        if row.get("score") is None
    ]
    if failures:
        lines.append("")
        lines.append(f"  unscored samples ({len(failures)}):")
        for row in failures[:20]:
            detail = row.get("error") or row.get("status") or "unknown"
            lines.append(f"    - {row.get('task_id')}: {detail}")
        if len(failures) > 20:
            lines.append(f"    ... and {len(failures) - 20} more")
    lines.append("=" * 62)
    return "\n".join(lines)
