"""Bounded, read-only JSON/SSE metadata observation; never stores response text."""
from __future__ import annotations

import json
from typing import Any


class ResponseMetadata:
    LIMIT = 1024 * 1024

    def __init__(self, content_type: str, content_encoding: str = "") -> None:
        self.sse = "text/event-stream" in content_type
        self.enabled = content_encoding in ("", "identity") and (self.sse or "json" in content_type)
        self.buffer = b""
        self.dropping = False
        self.partial = False
        self.models: list[str] = []
        self.reasons: list[str] = []
        self.statuses: list[str] = []

    def _add(self, values: list[str], value: Any) -> None:
        if isinstance(value, str) and value and value not in values:
            if len(value) <= 256 and len(values) < 32:
                values.append(value)
            else:
                self.partial = True

    def _parse(self, data: bytes) -> None:
        if data.strip() == b"[DONE]":
            return
        try:
            body = json.loads(data)
        except (ValueError, UnicodeError, RecursionError):
            self.partial = True
            return
        if not isinstance(body, dict):
            return
        # Chat Completions; Responses events; Anthropic message_start/delta.
        candidates = [body]
        candidates.extend(body[k] for k in ("response", "message", "delta") if isinstance(body.get(k), dict))
        for item in candidates:
            self._add(self.models, item.get("model"))
            self._add(self.reasons, item.get("stop_reason"))
            if item.get("status") in ("completed", "incomplete", "failed", "cancelled"):
                self._add(self.statuses, item["status"])
            details = item.get("incomplete_details")
            if item.get("status") == "incomplete" and isinstance(details, dict):
                self._add(self.reasons, details.get("reason"))
            choices = item.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    if isinstance(choice, dict):
                        self._add(self.reasons, choice.get("finish_reason"))

    def feed(self, chunk: bytes) -> None:
        if not self.enabled:
            return
        if not self.sse:
            if self.dropping or len(self.buffer) + len(chunk) > self.LIMIT:
                self.buffer = b""
                self.dropping = self.partial = True
            else:
                self.buffer += chunk
            return
        # Buffer only one bounded SSE line, never a complete streamed answer.
        parts = chunk.split(b"\n")
        for index, part in enumerate(parts):
            if not self.dropping:
                if len(self.buffer) + len(part) > self.LIMIT:
                    self.buffer = b""
                    self.dropping = self.partial = True
                else:
                    self.buffer += part
            if index < len(parts) - 1:
                if not self.dropping and self.buffer.startswith(b"data:"):
                    self._parse(self.buffer[5:].strip())
                self.buffer = b""
                self.dropping = False

    def finish(self) -> dict[str, Any]:
        if self.enabled and self.buffer:
            if not self.sse:
                self._parse(self.buffer)
            elif self.buffer.startswith(b"data:"):
                self._parse(self.buffer[5:].strip())
        self.buffer = b""
        status = "partial" if self.partial else "observed" if self.models or self.reasons or self.statuses else "not_observable"
        return {"response_models": self.models, "finish_reasons": self.reasons,
                "response_statuses": self.statuses, "capture_status": status}
