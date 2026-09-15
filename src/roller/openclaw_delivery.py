from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from roller.openclaw_usage import OpenClawUsage


@dataclass(frozen=True)
class DeliveryDecision:
    skip_message: bool = False
    skip_usage: bool = False
    # Incremental cost, after crediting both provider rows and prior mirrors.
    additional_cost_usd: float = 0.0


def _stop_reason(message: dict[str, Any]) -> Any:
    return message.get("stopReason", message.get("stop_reason"))


def _same_source(left: dict[str, Any], right: dict[str, Any]) -> bool:
    # Missing identifiers match only other missing identifiers in legacy raw.
    return all(left.get(key) == right.get(key) for key in ("provider", "model"))


def _text(message: dict[str, Any], *, plain_only: bool) -> str | None:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            parts.append(block["text"])
        elif (
            not plain_only
            and isinstance(block, dict)
            and block.get("type") in {"thinking", "reasoning"}
        ):
            continue
        else:
            # Never discard a CLI message carrying tools, media or new reasoning.
            return None
    return "\n".join(parts).strip()


@dataclass
class _ProviderRecord:
    message: dict[str, Any]
    usage: OpenClawUsage | None
    call_cost: float
    # Aggregate cost through this record, within the same provider/model.
    prefix_cost: float = 0.0


_MatchScope = Literal["last_call", "turn_total"]


def _match_usage(
    usage: OpenClawUsage | None, records: list[_ProviderRecord]
) -> _MatchScope | None:
    if usage is None or not usage.matchable or not any(usage.counts) or not records:
        return None
    last = records[-1].usage
    if last is not None and last.matchable and usage.counts == last.counts:
        return "last_call"
    known = [
        record.usage.counts for record in records
        if record.usage is not None and record.usage.matchable
    ]
    if len(known) == len(records):
        total = tuple(sum(column) for column in zip(*known))
        if usage.counts == total:
            return "turn_total"
        # OpenClaw 2026.7.1 buildUsageAgentMetaFields reports cumulative
        # input/output/cache but the LAST call's total (context size).
        # Accept this specific shape, not arbitrary mismatched totals.
        components, reported_total = usage.counts[:-1], usage.counts[-1]
        latest_total = known[-1][-1]
        if (
            any(components) and components == total[:-1]
            and latest_total > 0 and reported_total == latest_total
        ):
            return "turn_total"
    return None


def _accounted_cost(records: list[_ProviderRecord]) -> float:
    total = 0.0
    for record in records:
        total = max(total + record.call_cost, record.prefix_cost)
    return total


def _credit_cost(
    records: list[_ProviderRecord], scope: _MatchScope, cost: float
) -> float:
    """Credit only a new cost increment for the matched calls.

    Single-call and aggregate reports are overlapping cost floors, not
    separate charges. Prefix floors let an aggregate cover later per-call
    reports without absorbing costs of NEW calls beyond that prefix.
    Only tracker-owned state changes; raw provider usage stays untouched.
    """
    before = _accounted_cost(records)
    last = records[-1]
    if scope == "last_call":
        last.call_cost = max(last.call_cost, cost)
    else:
        last.prefix_cost = max(last.prefix_cost, cost)
    return max(_accounted_cost(records) - before, 0.0)


class OpenClawDeliveryTracker:
    """Project delivery copies without rewriting raw or inferring late delivery.

    Three invariants define the supported session grammar:
    - Only the latest provider's same-source plain CLI reply can be matched.
    - Independent CLI replies invalidate delivery eligibility, not accounting.
    - Only user/session/compaction boundaries clear the turn's provider ledger.

    Matched gap fills keep new text and update the delivered text, while their
    usage/cost stays attached to the matched provider calls. Cost never decides
    a match. Unknown CLI replies (including CLI-only backends) are retained.
    """

    def __init__(self) -> None:
        self._providers: list[_ProviderRecord] = []
        self._latest: dict[str, Any] | None = None
        self._delivered_text: str | None = None

    def _invalidate_delivery(self) -> None:
        self._latest = None
        self._delivered_text = None

    def _reset(self) -> None:
        self._providers.clear()
        self._invalidate_delivery()

    def observe(self, event: dict[str, Any]) -> DeliveryDecision:
        if event.get("type") in {"session", "compaction"}:
            self._reset()
        if event.get("type") != "message":
            return DeliveryDecision()
        message = event.get("message")
        if not isinstance(message, dict):
            return DeliveryDecision()
        if message.get("role") == "user":
            self._reset()
            return DeliveryDecision()
        if message.get("role") != "assistant":
            return DeliveryDecision()
        if message.get("api") != "cli":
            usage = OpenClawUsage.parse(message.get("usage"))
            self._providers.append(_ProviderRecord(
                message, usage, usage.cost_usd if usage is not None else 0.0,
            ))
            self._latest = message
            self._delivered_text = (
                _text(message, plain_only=False)
                if _stop_reason(message) in {None, "stop", "end_turn"} else None
            )
            return DeliveryDecision()

        latest = self._latest
        visible = _text(message, plain_only=True)
        if (
            latest is None or not _same_source(message, latest)
            or _stop_reason(latest) in {"error", "aborted"}
            or visible is None or _stop_reason(message) not in {None, "stop", "end_turn"}
        ):
            self._invalidate_delivery()
            return DeliveryDecision()

        # Keep failed calls in this ledger: a later successful retry's
        # aggregate can include their already recorded usage and cost.
        source_rows = [
            row for row in self._providers if _same_source(message, row.message)
        ]
        usage = OpenClawUsage.parse(message.get("usage"))
        scope = _match_usage(usage, source_rows)
        same_text = bool(visible and visible == self._delivered_text)
        no_usage = message.get("usage") is None or (
            usage is not None and usage.matchable and not any(usage.counts)
        )
        skip_message = same_text and (scope is not None or no_usage)
        if skip_message and scope is None:
            # No counters means no token evidence, not no cost. The text
            # match identifies the latest call for a cost-only delivery.
            scope = "last_call"
        if scope is None:
            self._invalidate_delivery()
            return DeliveryDecision()

        additional_cost = (
            _credit_cost(source_rows, scope, usage.cost_usd)
            if usage is not None else 0.0
        )
        self._delivered_text = visible
        return DeliveryDecision(
            skip_message=skip_message,
            skip_usage=True,
            additional_cost_usd=additional_cost,
        )
