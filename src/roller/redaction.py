"""Bounded-memory literal secret redaction for raw traces and runtime logs."""
from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
from typing import Iterable


def redact_file_literals(
    path: Path,
    secrets: Iterable[str],
    *,
    errors: str = "strict",
    chunk_size: int = 64 * 1024,
) -> None:
    """Atomically redact literals, including matches spanning chunks/newlines.

    Memory is bounded by one chunk plus the longest secret, not by file/line
    length. On decoding or I/O failure the original file remains untouched.
    """
    values = sorted({value for value in secrets if value}, key=len, reverse=True)
    if not values:
        return
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    pattern = re.compile("|".join(re.escape(value) for value in values))
    overlap = max(map(len, values)) - 1
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as destination:
            with path.open(encoding="utf-8", errors=errors, newline="") as source:
                pending = ""
                while chunk := source.read(chunk_size):
                    pending += chunk
                    boundary = max(0, len(pending) - overlap)
                    for match in pattern.finditer(pending):
                        if match.start() < boundary < match.end():
                            boundary = match.start()
                            break
                    destination.write(pattern.sub("[REDACTED]", pending[:boundary]))
                    pending = pending[boundary:]
                destination.write(pattern.sub("[REDACTED]", pending))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
