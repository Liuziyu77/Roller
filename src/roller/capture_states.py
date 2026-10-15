"""Values of the four independent per-call capture state fields.

These are plain strings on disk for compatibility with roller.call/v1.
"""
from __future__ import annotations

from typing import Final, Literal


CallExecutionState = Literal["completed", "failed"]
CaptureState = Literal["complete", "partial"]
ExportState = Literal["available", "unavailable"]
IdentityState = Literal["observed", "unknown"]


class CallExecutionStatus:
    COMPLETED: Final[CallExecutionState] = "completed"
    FAILED: Final[CallExecutionState] = "failed"
    VALUES = frozenset((COMPLETED, FAILED))


class CaptureStatus:
    COMPLETE: Final[CaptureState] = "complete"
    PARTIAL: Final[CaptureState] = "partial"
    VALUES = frozenset((COMPLETE, PARTIAL))


class ExportStatus:
    AVAILABLE: Final[ExportState] = "available"
    UNAVAILABLE: Final[ExportState] = "unavailable"
    VALUES = frozenset((AVAILABLE, UNAVAILABLE))


class IdentityStatus:
    OBSERVED: Final[IdentityState] = "observed"
    UNKNOWN: Final[IdentityState] = "unknown"
    VALUES = frozenset((OBSERVED, UNKNOWN))
