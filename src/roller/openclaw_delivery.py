from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DeliveryDecision:
    skip_message: bool = False
    skip_usage: bool = False


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


def _counts(usage: Any) -> tuple[int | float, ...] | None:
    if not isinstance(usage, dict):
        return None
    values = tuple(
        usage.get(key, 0) for key in ("input", "output", "cacheRead", "cacheWrite")
    )
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
        for value in values
    ):
        return None
    total = usage.get("totalTokens", sum(values))
    if isinstance(total, bool) or not isinstance(total, (int, float)) or total < 0:
        return None
    return (*values, total)


class OpenClawDeliveryTracker:
    """Recognize CLI mirrors using preceding events in the same user turn.

    ``api=cli`` also marks genuine CLI-backend replies and embedded gap fills.
    Keep their content unless the latest provider reply already contains it.
    A gap fill can carry that call's usage or the whole turn's accumulated
    usage; suppress only accounting that matches recorded provider counts.
    Ambiguous events are retained. No raw event is changed.
    """

    def __init__(self) -> None:
        self._providers: list[dict[str, Any]] = []
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
            self._providers.append(message)
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
        candidates = [
            row for row in self._providers
            if same_source(row)
            and row.get("stopReason", row.get("stop_reason")) not in {"error", "aborted"}
        ]
        counts = _counts(message.get("usage"))
        recorded = [_counts(row.get("usage")) for row in candidates]
        repeated_usage = False
        if counts is not None and any(counts) and recorded:
            repeated_usage = counts == recorded[-1]
            known_counts = [item for item in recorded if item is not None]
            if len(known_counts) == len(recorded):
                total = tuple(
                    sum(item[index] for item in known_counts)
                    for index in range(len(counts))
                )
                repeated_usage = repeated_usage or counts == total
        latest = self._latest
        same_text = bool(
            visible and latest is not None and same_source(latest)
            and latest.get("stopReason", latest.get("stop_reason"))
            in {None, "stop", "end_turn"}
            and visible == _text(latest, plain_only=False)
        )
        no_usage = message.get("usage") is None or (
            counts is not None and not any(counts)
        )
        skip_message = same_text and (repeated_usage or no_usage)
        if not skip_message:
            # A retained CLI reply is a new boundary, not a provider response
            # against which later independent CLI invocations can be deduped.
            self._reset()
        return DeliveryDecision(skip_message, repeated_usage or skip_message)
