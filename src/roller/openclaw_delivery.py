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
    """Recognize CLI mirrors using preceding events in the same user turn.

    ``api=cli`` also marks genuine CLI-backend replies and embedded gap fills.
    Keep their content unless the latest provider reply already contains it.
    A gap fill can carry that call's usage or the whole turn's accumulated
    usage; suppress only accounting that matches recorded provider counts.
    Cost on a suppressed mirror is credited once across overlapping reports.
    Cost never decides the match itself:
    aggregate mirrors omit it and serialized floats only match by luck.
    Ambiguous events are retained. No raw event is changed.
    """

    def __init__(self) -> None:
        self._providers: list[_ProviderRecord] = []
        self._latest: dict[str, Any] | None = None

    def _reset(self) -> None:
        self._providers.clear()
        self._latest = None

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
            return DeliveryDecision()

        def same_source(other: dict[str, Any]) -> bool:
            # Missing identifiers match only other missing identifiers, for
            # legacy traces. Never infer an identity from just one side.
            return all(
                message.get(key) == other.get(key) for key in ("provider", "model")
            )

        # Delivery mirrors are plain final replies. Failed or structured CLI
        # responses are not evidence of an already-accounted model request.
        visible = _text(message, plain_only=True)
        if visible is None or message.get(
            "stopReason", message.get("stop_reason")
        ) not in {None, "stop", "end_turn"}:
            self._reset()
            return DeliveryDecision()
        source_rows = [
            row for row in self._providers if same_source(row.message)
        ]
        # A CLI reply following a failed same-source call is a fallback for
        # that failure, not a delivery mirror of an earlier success; the
        # failure is a boundary, so nothing before it may absorb its usage.
        candidates = [] if source_rows and source_rows[-1].message.get(
            "stopReason", source_rows[-1].message.get("stop_reason")
        ) in {"error", "aborted"} else [
            row for row in source_rows
            if row.message.get("stopReason", row.message.get("stop_reason"))
            not in {"error", "aborted"}
        ]
        usage = OpenClawUsage.parse(message.get("usage"))
        scope = _match_usage(usage, candidates)
        latest = self._latest
        same_text = bool(
            visible and latest is not None and same_source(latest)
            and latest.get("stopReason", latest.get("stop_reason"))
            in {None, "stop", "end_turn"}
            and visible == _text(latest, plain_only=False)
        )
        no_usage = message.get("usage") is None or (
            usage is not None and usage.matchable and not any(usage.counts)
        )
        skip_message = same_text and (scope is not None or no_usage)
        if skip_message and scope is None:
            # No counters means no token evidence, not no cost. The text
            # match identifies the latest call for a cost-only delivery.
            scope = "last_call"
        additional_cost = (
            _credit_cost(candidates, scope, usage.cost_usd)
            if scope is not None and usage is not None else 0.0
        )
        if not skip_message:
            # A retained CLI reply is a new boundary, not a provider response
            # against which later independent CLI invocations can be deduped.
            self._reset()
        return DeliveryDecision(
            skip_message=skip_message,
            skip_usage=scope is not None,
            additional_cost_usd=additional_cost,
        )
