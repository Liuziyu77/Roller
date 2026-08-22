"""Resolved, secret-safe run lock used to make resume reproducible."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from roller.models import RunRequest
from roller.storage import atomic_write_json
from roller.wire_trace import redact_sensitive_values


RUN_LOCK_SCHEMA = "roller.run-lock/v1"
LEGACY_MIGRATION_FILENAME = ".legacy-resume-untrusted.json"
URL_KEY_NAMES = {"baseurl", "apiurl", "endpoint", "url"}
SENSITIVE_QUERY_NAMES = {
    "accesskey",
    "accesstoken",
    "apikey",
    "auth",
    "authorization",
    "code",
    "credential",
    "key",
    "password",
    "refreshtoken",
    "secret",
    "sig",
    "signature",
    "token",
}


@dataclass(frozen=True)
class RunLockDecision:
    reuse_existing_samples: bool
    created: bool
    legacy_run: bool


def _sanitize_lock_values(value: Any, key_name: str = "") -> Any:
    """Redact credentials while retaining non-secret URL routing semantics."""

    redacted = redact_sensitive_values(value)
    if isinstance(redacted, dict):
        return {
            key: _sanitize_lock_values(item, str(key))
            for key, item in redacted.items()
        }
    if isinstance(redacted, list):
        return [_sanitize_lock_values(item, key_name) for item in redacted]
    normalized = "".join(
        character for character in key_name.lower() if character.isalnum()
    )
    if isinstance(redacted, str) and normalized in URL_KEY_NAMES:
        try:
            parsed = urlsplit(redacted)
            port = parsed.port
        except ValueError:
            return "<invalid-url>"
        hostname = parsed.hostname or ""
        if ":" in hostname:
            hostname = f"[{hostname}]"
        netloc = hostname
        if port is not None:
            netloc = f"{netloc}:{port}"
        query: list[tuple[str, str]] = []
        for key, item in parse_qsl(parsed.query, keep_blank_values=True):
            query_key = "".join(
                character for character in key.lower() if character.isalnum()
            )
            sensitive = (
                query_key in SENSITIVE_QUERY_NAMES
                or query_key.endswith("apikey")
                or query_key.endswith("token")
                or query_key.endswith("signature")
            )
            query.append((key, "<redacted>" if sensitive else item))
        return urlunsplit(
            (
                parsed.scheme,
                netloc if parsed.netloc else "",
                parsed.path,
                urlencode(query),
                "",
            )
        )
    return redacted


def build_run_lock(
    requests: Sequence[RunRequest],
    *,
    roller_version: str,
    execution: Mapping[str, Any],
    model_config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build a stable lock without persisting credentials.

    Task/input/context identity reuses the content digests already calculated
    by ``TaskBundleStore``. Small configuration objects are compared directly;
    hashing them again would add no useful property.
    """

    locked_requests: list[dict[str, Any]] = []
    for request in requests:
        if request.bundle is None:
            raise ValueError("run lock requires materialized task bundles")
        locked_requests.append(
            {
                "request_index": request.index,
                "source_index": request.task.source_index,
                "task_id": request.task.id,
                "model": request.model,
                "timeout_seconds": request.timeout_seconds,
                "task_bundle": {
                    "manifest_digest": request.bundle.manifest_digest,
                    "input_tree_digest": request.bundle.input_tree_digest,
                    "input_manifest_digest": request.bundle.input_manifest_digest,
                    "context_manifest_digest": request.bundle.context_manifest_digest,
                    "resume_safe": request.bundle.resume_safe,
                },
            }
        )
    return {
        "schema_version": RUN_LOCK_SCHEMA,
        "roller_version": roller_version,
        "execution": _sanitize_lock_values(dict(execution)),
        "model_config": _sanitize_lock_values(
            dict(model_config) if model_config is not None else None
        ),
        "requests": locked_requests,
    }


def _short(value: Any, limit: int = 160) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def _differences(recorded: Any, current: Any, path: str = "$", limit: int = 8) -> list[str]:
    if recorded == current:
        return []
    if limit <= 0:
        return [f"{path}: additional differences omitted"]
    if isinstance(recorded, dict) and isinstance(current, dict):
        rows: list[str] = []
        for key in sorted(set(recorded) | set(current), key=str):
            child = f"{path}.{key}"
            if key not in recorded:
                rows.append(f"{child}: only in current lock")
            elif key not in current:
                rows.append(f"{child}: missing from current lock")
            else:
                rows.extend(
                    _differences(
                        recorded[key], current[key], child, limit - len(rows)
                    )
                )
            if len(rows) >= limit:
                break
        return rows
    if isinstance(recorded, list) and isinstance(current, list):
        rows = []
        for index in range(max(len(recorded), len(current))):
            child = f"{path}[{index}]"
            if index >= len(recorded):
                rows.append(f"{child}: only in current lock")
            elif index >= len(current):
                rows.append(f"{child}: missing from current lock")
            else:
                rows.extend(
                    _differences(
                        recorded[index], current[index], child, limit - len(rows)
                    )
                )
            if len(rows) >= limit:
                break
        return rows
    return [
        f"{path}: recorded={_short(recorded)}, current={_short(current)}"
    ]


def ensure_run_lock(
    path: Path,
    expected: Mapping[str, Any],
    *,
    resume: bool,
    existing_run: bool,
) -> RunLockDecision:
    """Write a new lock or reject resume against a different resolved run.

    Legacy runs without a lock are adopted, but their old samples are not
    reused on that first adoption because their execution configuration cannot
    be proven equivalent.
    """

    expected_row = dict(expected)
    migration_path = path.with_name(LEGACY_MIGRATION_FILENAME)
    if path.is_file():
        try:
            recorded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"existing run lock cannot be parsed; refusing to overwrite {path}: {exc}"
            ) from exc
        if recorded != expected_row:
            details = "\n".join(f"- {row}" for row in _differences(recorded, expected_row))
            raise FileExistsError(
                "run directory cannot be resumed with different resolved inputs; "
                f"lock mismatch at {path}:\n{details}"
            )
        return RunLockDecision(
            reuse_existing_samples=bool(resume and not migration_path.is_file()),
            created=False,
            legacy_run=migration_path.is_file(),
        )

    legacy = bool(resume and existing_run)
    if legacy:
        # Write the marker first so a hard stop cannot leave an apparently
        # trusted lock in front of samples created under an unknown old config.
        atomic_write_json(
            migration_path,
            {
                "schema_version": "roller.legacy-resume-untrusted/v1",
                "reason": "existing run was created without lock.json",
            },
        )
    atomic_write_json(path, expected_row)
    return RunLockDecision(
        reuse_existing_samples=bool(resume and not legacy),
        created=True,
        legacy_run=legacy,
    )


def complete_legacy_adoption(path: Path) -> None:
    """Mark an unlocked legacy run trusted after every request was re-executed."""

    path.with_name(LEGACY_MIGRATION_FILENAME).unlink(missing_ok=True)
