"""Explicit native input images, independent of file mount/copy and tool vision."""
from __future__ import annotations

import base64
from pathlib import Path, PurePosixPath

from roller.models import RunRequest

MIMES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".webp": "image/webp", ".gif": "image/gif"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def attachment_specs(request: RunRequest, workspace: str, *, first_turn: bool) -> list[dict[str, str]]:
    if not first_turn:
        return []
    images = []
    seen = set()
    for value in request.task.attach_images:
        path = PurePosixPath(value)
        if (not value or any(c in value for c in "\r\n") or path.is_absolute()
                or ".." in path.parts or value in seen or str(path) != value):
            raise ValueError("native image paths must be unique, safe workspace-relative paths")
        if path.suffix.lower() not in MIMES:
            raise ValueError(f"unsupported native image extension: {value}")
        if not any(value == item.path or value.startswith(item.path.rstrip("/") + "/")
                   or item.path == "." for item in request.task.inputs):
            raise ValueError(f"native image is not a declared task input: {value}")
        images.append({"path": str(PurePosixPath(workspace) / path),
                       "relative_path": value, "mime_type": MIMES[path.suffix.lower()]})
        seen.add(value)
    return images


def attachment_source(request: RunRequest, relative: str) -> Path:
    """Resolve snapshotted bytes, including inputs with no original source path."""
    if request.bundle is not None:
        root = request.bundle.inputs_root.resolve()
        path = (root / relative).resolve()
        path.relative_to(root)
    else:
        path = None
        for item in request.task.inputs:
            if item.source is None:
                continue
            if relative == item.path:
                path = item.source
                break
            if item.path == "." or relative.startswith(item.path.rstrip("/") + "/"):
                root = item.source.resolve()
                suffix = relative if item.path == "." else relative[len(item.path.rstrip("/")) + 1:]
                path = (root / suffix).resolve()
                path.relative_to(root)
                break
        if path is None:
            raise ValueError(f"native image requires a materialized task bundle: {relative}")
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
        raise ValueError(f"native image is missing, empty or exceeds {MAX_IMAGE_BYTES} bytes: {relative}")
    return path


def claude_message(request: RunRequest, message: str, *, first_turn: bool) -> dict:
    content = [{"type": "text", "text": message}]
    for image in attachment_specs(request, "/", first_turn=first_turn):
        source = attachment_source(request, image["relative_path"])
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": image["mime_type"],
            "data": base64.b64encode(source.read_bytes()).decode("ascii"),
        }})
    return {"type": "user", "message": {"role": "user", "content": content}}


def openclaw_message(request: RunRequest, message: str, workspace: str, *, first_turn: bool) -> str:
    markers = []
    for image in attachment_specs(request, workspace, first_turn=first_turn):
        if any(c in image["path"] for c in "[]\r\n"):
            raise ValueError("native OpenClaw media marker cannot encode this path")
        markers.append(f'[media attached: {image["path"]} ({image["mime_type"]})]')
    return "\n".join([*markers, message])
