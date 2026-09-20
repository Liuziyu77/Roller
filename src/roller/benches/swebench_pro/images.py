"""Docker image composition for the SWE-Bench Pro harness."""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

IMAGE_PULL_ATTEMPTS = 5
IMAGE_PULL_RETRY_SECONDS = 15
_IMAGE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]*$")
_BUILD_LOCK = threading.Lock()
_DERIVED_IMAGES: dict[tuple[str, str, str, str], str] = {}
_HARNESS_IMAGE_REVISIONS = {"openclaw": "isolated-node-v7"}


def _image_id(runtime: Any, image: str) -> str:
    inspected = runtime.run(
        ["image", "inspect", image, "--format", "{{.Id}}"], timeout=60
    )
    if inspected.returncode != 0:
        detail = ""
        for attempt in range(1, IMAGE_PULL_ATTEMPTS + 1):
            logger.info(
                "pulling SWE-Bench Pro runtime image %s (attempt %s/%s)",
                image,
                attempt,
                IMAGE_PULL_ATTEMPTS,
            )
            try:
                pulled = runtime.run(["pull", image], timeout=3600)
            except subprocess.TimeoutExpired as exc:
                detail = str(exc)
            else:
                detail = (pulled.stderr or pulled.stdout or "").strip()[-2000:]
            inspected = runtime.run(
                ["image", "inspect", image, "--format", "{{.Id}}"], timeout=60
            )
            if inspected.returncode == 0 and inspected.stdout.strip():
                break
            if attempt < IMAGE_PULL_ATTEMPTS:
                delay = min(
                    IMAGE_PULL_RETRY_SECONDS * (2 ** (attempt - 1)),
                    120,
                )
                logger.warning(
                    "Docker image pull failed for %s; retrying in %ss: %s",
                    image,
                    delay,
                    detail,
                )
                time.sleep(delay)
        else:
            raise RuntimeError(f"could not pull Docker image {image}: {detail}")
    if inspected.returncode != 0 or not inspected.stdout.strip():
        detail = (inspected.stderr or inspected.stdout or "").strip()[-1000:]
        raise RuntimeError(f"could not inspect Docker image {image}: {detail}")
    return inspected.stdout.strip()


def ensure_harness_image(
    runtime: Any,
    source_image: str,
    harness_image: str,
    harness_name: str,
) -> str:
    """Build an Agent image with one Harness and the official repository snapshot."""

    for image in (source_image, harness_image):
        if not _IMAGE_REF_RE.fullmatch(image):
            raise ValueError(f"unsafe Docker image reference: {image!r}")
    with _BUILD_LOCK:
        source_id = _image_id(runtime, source_image)
        harness_id = _image_id(runtime, harness_image)
        revision = _HARNESS_IMAGE_REVISIONS.get(harness_name, "v1")
        key = (source_id, harness_id, harness_name, revision)
        cached = _DERIVED_IMAGES.get(key)
        if cached is not None:
            return cached
        digest = hashlib.sha256("\0".join(key).encode()).hexdigest()[:20]
        tag = f"roller-swebench-pro-{harness_name}:{digest}"
        existing = runtime.run(
            ["image", "inspect", tag, "--format", "{{.Id}}"], timeout=60
        )
        if existing.returncode == 0:
            _DERIVED_IMAGES[key] = tag
            return tag
        if harness_name == "openclaw":
            dockerfile = f"""\
FROM {harness_image} AS harness
FROM {source_image}
USER root
COPY --from=harness /usr/local/bin/node /opt/roller-node/bin/node.real
COPY --from=harness /lib/x86_64-linux-gnu /opt/roller-glibc
COPY --from=harness /app /opt/roller-openclaw
RUN if /opt/roller-node/bin/node.real --version >/dev/null 2>&1; then \\
        mv /opt/roller-node/bin/node.real /opt/roller-node/bin/node; \\
        rm -rf /opt/roller-glibc; \\
    else \\
        printf '%s\\n' '#!/bin/sh' \\
            'exec /opt/roller-glibc/ld-linux-x86-64.so.2 --library-path /opt/roller-glibc /opt/roller-node/bin/node.real "$@"' \\
            > /opt/roller-node/bin/node; \\
        chmod 755 /opt/roller-node/bin/node; \\
    fi \\
    && printf '%s\\n' '#!/bin/sh' 'OPENCLAW_NO_RESPAWN=1 exec /opt/roller-node/bin/node /opt/roller-openclaw/openclaw.mjs "$@"' > /usr/local/bin/openclaw \\
    && chmod 755 /usr/local/bin/openclaw /opt/roller-node/bin/node \\
    && /opt/roller-node/bin/node --version
ENV PATH="/usr/local/bin:${{PATH}}"
ENTRYPOINT []
CMD ["/bin/bash"]
"""
        else:
            dockerfile = f"""\
FROM {source_image} AS instance
FROM {harness_image}
USER root
COPY --from=instance /app /app
ENTRYPOINT []
CMD ["/bin/bash"]
"""
        with tempfile.TemporaryDirectory(prefix="roller-swebench-image-") as temporary:
            context = Path(temporary)
            (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            built = runtime.run(["build", "-t", tag, str(context)], timeout=3600)
        if built.returncode != 0:
            detail = (built.stderr or built.stdout or "").strip()[-4000:]
            raise RuntimeError(
                f"could not combine {harness_name} with SWE-Bench Pro image "
                f"{source_image}: {detail}"
            )
        _DERIVED_IMAGES[key] = tag
        return tag
