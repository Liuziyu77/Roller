"""Validate persisted image payloads without opening native paths or networks."""
from __future__ import annotations

import base64
import binascii
import hashlib
from typing import Any

IMAGE_MIME_TYPES = {"image/gif", "image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 50 * 1024 * 1024


def journal_image(data: Any, mime: Any, *, sha256: Any = None) -> dict[str, str]:
    if mime not in IMAGE_MIME_TYPES:
        raise ValueError("Attachment has an unsupported image MIME type")
    if not isinstance(data, str) or not data or len(data) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ValueError("Attachment image payload is empty or exceeds the byte limit")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Attachment image payload is not valid base64") from exc
    if not 0 < len(decoded) <= MAX_IMAGE_BYTES:
        raise ValueError("Attachment image size is outside the supported range")
    if sha256 is not None and sha256 != hashlib.sha256(decoded).hexdigest():
        raise ValueError("Attachment image SHA256 does not match journal bytes")
    return {"type": "image", "mimeType": mime, "data": base64.b64encode(decoded).decode("ascii")}
