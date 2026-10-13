"""HTTP call recording and capture artifact integrity.

The HTTP recorder never guesses an agent from timing or prompt text. Attribution
is resolved after collection using response IDs and native assistant IDs.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

logger = logging.getLogger(__name__)

def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError(f"non-object record in {path.name}")
                rows.append(row)
    return rows


class CallRecorder:
    """Append requests and response completion events; stream bodies to disk."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events = directory / "events.jsonl"
        self.error: str | None = None
        self.request_seqs: set[int] = set()
        self.completed_seqs: set[int] = set()
        self.sealed = False
        self.scope_id = uuid.uuid4().hex
        self._streams: list[BinaryIO] = []
        self._lock = threading.RLock()

    def write(self, row: dict[str, Any]) -> None:
        try:
            row = {"schema_version": "roller.capture-event/v1",
                   "timestamp": datetime.now(timezone.utc).isoformat(), **row}
            with self._lock:
                if self.sealed:
                    return
                with self.events.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as exc:
            self.error = str(exc)

    def begin(self, seq: int, path: str, body: dict[str, Any], *, native_identity: dict[str, str] | None = None) -> str:
        call_id = f"{self.scope_id}:{self.directory.name}:call-{seq:08d}"
        with self._lock:
            if self.sealed:
                return call_id
            self.request_seqs.add(seq)
            self.write({"type": "request", "call_id": call_id,
                        "request_seq": seq, "path": path, "body": body,
                        **({"native_identity": native_identity} if native_identity else {})})
        return call_id

    def response_path(self, seq: int) -> Path:
        directory = self.directory / "responses"
        directory.mkdir(exist_ok=True)
        return directory / f"call-{seq:08d}.body"

    def finish(self, seq: int, *, status: int | None, content_type: str,
               content_encoding: str, complete: bool, error: str | None = None) -> None:
        with self._lock:
            self.write({"type": "response", "request_seq": seq,
                        "http_status": status, "content_type": content_type,
                        "content_encoding": content_encoding, "complete": complete,
                        "error": error, "response_path": f"responses/call-{seq:08d}.body"})
            self.completed_seqs.add(seq)

    def open_response(self, seq: int) -> BinaryIO:
        with self._lock:
            if self.sealed:
                raise RuntimeError("capture recorder is sealed")
            stream = self.response_path(seq).open("wb")
            self._streams.append(stream)
            return stream

    def append_response(self, stream: BinaryIO, chunk: bytes) -> None:
        with self._lock:
            if not self.sealed:
                stream.write(chunk)
                stream.flush()

    def seal(self) -> None:
        """Freeze artifacts before hashing, even if background calls remain."""
        with self._lock:
            self.sealed = True
            for stream in self._streams:
                try:
                    stream.close()
                except OSError as exc:
                    self.error = self.error or str(exc)
            if self.request_seqs - self.completed_seqs:
                self.error = self.error or "HTTP responses still in flight at capture shutdown"


def validate_capture(sample: Path, metadata: dict[str, Any]) -> None:
    try:
        _validate_capture(sample, metadata)
    except (OSError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"invalid capture artifacts: {exc}") from exc


def _validate_capture(sample: Path, metadata: dict[str, Any]) -> None:
    relative = metadata.get("manifest")
    if not isinstance(relative, str):
        raise ValueError("capture manifest reference is missing")
    path = _safe_file(sample, relative)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "roller.capture/v1":
        raise ValueError("unsupported capture manifest")
    if manifest.get("capture_status") not in {"complete", "partial"}:
        raise ValueError("invalid capture status")
    if metadata.get("capture_status") != manifest.get("capture_status"):
        raise ValueError("capture status does not match manifest")
    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        raise ValueError("capture manifest has no file inventory")
    inventory = {record["path"] for record in records}
    if not {"calls.jsonl", "relations.jsonl"}.issubset(inventory):
        raise ValueError("capture manifest lacks call/relation inventory")
    for record in records:
        target = _safe_file(path.parent, record["path"])
        if hashlib.sha256(target.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError(f"capture file hash mismatch: {record['path']}")
    calls = read_jsonl(path.parent / "calls.jsonl")
    if len(calls) != manifest.get("call_count"):
        raise ValueError("capture call count mismatch")
    ids = [call.get("call_id") for call in calls]
    if any(not isinstance(cid, str) or not cid for cid in ids) or len(set(ids)) != len(ids):
        raise ValueError("capture call IDs are missing or duplicated")
    layout = manifest.get("storage_layout")
    if layout not in {None, "compact/v1", "compact/v2"}:
        raise ValueError("unsupported capture storage layout")
    lean = layout == "compact/v2" and manifest.get("raw_retained") is False
    if layout == "compact/v2":
        if manifest.get("debug_retention") not in {"off", "on-error", "full"} or not isinstance(manifest.get("raw_retained"), bool):
            raise ValueError("invalid capture retention policy")
        if lean and any(p.startswith("raw/") for p in inventory):
            raise ValueError("lean capture inventories raw files")
    if layout in {"compact/v1", "compact/v2"} and not lean:
        if "raw/events.jsonl" not in inventory:
            raise ValueError("compact capture lacks event inventory")
        requests = [e for e in read_jsonl(path.parent / "raw/events.jsonl") if e.get("type") == "request"]
        by_id = {c["call_id"]: c for c in calls}
        request_ids = [e.get("call_id") for e in requests]
        if len(request_ids) != len(calls) or set(request_ids) != set(ids):
            raise ValueError("compact request references do not match calls")
        for event in requests:
            cid = event["call_id"]
            if "body" in event or event.get("body_ref") != {"path": "calls.jsonl", "call_id": cid, "field": "context"}:
                raise ValueError("invalid compact context reference")
            if event.get("request_seq") != by_id[cid]["request_seq"] or event.get("path") != by_id[cid]["request_path"]:
                raise ValueError("compact request metadata does not match call")
    participants = manifest.get("participants", [])
    pids = {part["participant_id"] for part in participants}
    if len(pids) != len(participants):
        raise ValueError("duplicate capture participant IDs")
    if any(not isinstance(pid, str) or not pid for pid in pids):
        raise ValueError("invalid capture participant ID")
    for part in participants:
        if any(p not in inventory for p in part.get("native_files", [])):
            raise ValueError("participant references unregistered capture files")
        expected = [c for c in calls if c.get("participant_id") == part["participant_id"]]
        if layout not in {"compact/v1", "compact/v2"}:
            if part.get("calls_path") not in inventory:
                raise ValueError("participant references unregistered capture files")
            actual = read_jsonl(_safe_file(path.parent, part["calls_path"]))
            if actual != expected:
                raise ValueError("participant calls do not match capture calls")
        if part.get("call_ids") != [c["call_id"] for c in expected]:
            raise ValueError("participant call IDs do not match capture calls")
    for call in calls:
        if lean and (call.get("response_path") is not None or call.get("response_evidence_retained") is not False):
            raise ValueError("lean call references raw response")
        if call.get("schema_version") != "roller.call/v1" or not isinstance(call.get("context"), dict):
            raise ValueError("invalid capture call schema")
        if call.get("protocol") not in {"anthropic-messages", "openai-chat-completions", "openai-responses"}:
            raise ValueError("unsupported capture call protocol")
        if call.get("participant_id") is not None and call["participant_id"] not in pids:
            raise ValueError("call participant is missing")
        if call.get("capture_status") == "complete":
            if (not lean and call.get("response_path") not in inventory) or not isinstance(call.get("output"), dict):
                raise ValueError("complete call lacks response evidence")
        if call.get("export_status") == "available" and (
            call.get("capture_status") != "complete" or call.get("identity_status") != "observed"
            or call.get("response_capture_status") != "complete" or call.get("execution_status") != "completed"
            or call.get("output_source") != "model" or call.get("participant_id") not in pids
            or call.get("errors")):
            raise ValueError("available call lacks complete model/identity evidence")
    relations = read_jsonl(path.parent / "relations.jsonl")
    if len(relations) != manifest.get("relation_count"):
        raise ValueError("capture relation count mismatch")
    relation_ids = [r.get("relation_id") for r in relations]
    if any(not isinstance(rid, str) or not rid for rid in relation_ids) or len(set(relation_ids)) != len(relation_ids):
        raise ValueError("capture relation IDs are missing or duplicated")
    for relation in relations:
        if relation.get("schema_version") != "roller.relation/v1":
            raise ValueError("invalid capture relation schema")
        if relation.get("type") not in {"spawn", "result_delivered", "child_completed", "child_failed",
                                        "result_in_context", "launch_acknowledged", "launch_ack_in_context"}:
            raise ValueError("unsupported capture relation type")
        for key in ("parent_call_id", "call_id"):
            if relation.get(key) is not None and relation[key] not in ids:
                raise ValueError("relation call reference is missing")
        for key in ("parent_participant_id", "child_participant_id"):
            if relation.get(key) is not None and relation[key] not in pids:
                raise ValueError("relation participant is missing")
        for evidence in relation.get("evidence", []):
            if lean:
                if evidence.get("retained") is not False or "path" in evidence or not isinstance(evidence.get("source"), str):
                    raise ValueError("invalid released relation evidence")
            elif evidence.get("path") not in inventory:
                raise ValueError("relation evidence file is missing")
    lifecycle = manifest.get("lifecycle")
    if lifecycle is not None:
        if not isinstance(lifecycle, dict) or lifecycle.get("status") not in {"complete", "pending", "unsupported", "unknown"}:
            raise ValueError("invalid capture lifecycle")
        pending = lifecycle.get("pending_participant_ids")
        if not isinstance(pending, list) or any(not isinstance(pid, str) or pid not in pids for pid in pending) or len(set(pending)) != len(pending):
            raise ValueError("invalid pending lifecycle participants")
        if lifecycle["status"] == "complete" and pending:
            raise ValueError("complete lifecycle contains pending participants")
    lifecycle_incomplete = (lifecycle["status"] != "complete") if lifecycle is not None else manifest.get("background_detected")
    if manifest.get("capture_status") == "complete" and (
        not calls or manifest.get("recorder_error") or manifest.get("native_errors") or lifecycle_incomplete
        or any(c.get("capture_status") != "complete" for c in calls)
        or any(p.get("capture_status") != "complete" for p in participants)
        or any(r.get("status") != "observed" for r in relations)):
        raise ValueError("complete manifest contains incomplete evidence")


def _safe_file(base: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("capture path escapes sample")
    current = base
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("capture file must not use symlinks")
    if not current.is_file():
        raise ValueError(f"capture file missing: {relative}")
    return current


def native_directory(directory: Path) -> Path:
    """Read native evidence in legacy or compact captures."""
    legacy = directory / "native"
    return legacy if legacy.exists() else directory / "raw/native"


def _retention_paths(directory: Path) -> tuple[Path, Path]:
    return (directory.with_name(f".{directory.name}.retention-stage"),
            directory.with_name(f".{directory.name}.retention-backup"))


def _validated_manifest(directory: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        status = manifest["capture_status"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"capture retention recovery has no valid manifest in {directory}") from exc
    validate_capture(directory, {"manifest": "manifest.json", "capture_status": status})
    return manifest


def recover_capture_retention(directory: Path, *, prefer_raw: bool = False) -> bool:
    """Complete or roll back an interrupted compact/v2 directory publication."""

    stage, backup = _retention_paths(directory)
    if any(path.is_symlink() for path in (directory, stage, backup)):
        raise ValueError("capture retention transaction must not contain symlinks")
    if not stage.exists() and not backup.exists():
        return False

    def valid(path: Path) -> bool:
        try:
            _validated_manifest(path)
        except ValueError:
            return False
        return True

    if backup.exists():
        live_valid = directory.exists() and valid(directory)
        backup_valid = valid(backup)
        if backup_valid and (prefer_raw or not live_valid):
            if directory.exists():
                shutil.rmtree(directory)
            backup.rename(directory)
        elif live_valid:
            shutil.rmtree(backup)
        elif stage.exists() and valid(stage):
            if directory.exists():
                shutil.rmtree(directory)
            stage.rename(directory)
            shutil.rmtree(backup)
        else:
            raise ValueError(f"capture retention has no valid attempt to restore: {directory}")
    elif not directory.exists() and stage.exists() and valid(stage):
        stage.rename(directory)
    elif not directory.exists():
        raise ValueError(f"capture retention stage has no valid attempt: {directory}")
    else:
        _validated_manifest(directory)
    if stage.exists():
        shutil.rmtree(stage)
    return True


def apply_capture_retention(directory: Path, manifest: dict[str, Any], mode: str) -> dict[str, Any]:
    """Check the full capture before publishing optional debug evidence."""
    from roller.storage import atomic_write_json, write_jsonl

    if mode not in {"off", "on-error", "full"}:
        raise ValueError("invalid capture debug retention")
    recover_capture_retention(directory)
    validate_capture(directory, {"manifest": "manifest.json", "capture_status": manifest["capture_status"]})
    calls = read_jsonl(directory / "calls.jsonl")
    relations = read_jsonl(directory / "relations.jsonl")
    failed = (manifest["capture_status"] != "complete" or manifest["execution_status"] != "ok"
              or any(c["execution_status"] == "failed" for c in calls)
              or any(r["type"] == "child_failed" for r in relations))
    retain = mode == "full" or (mode == "on-error" and failed)
    manifest = deepcopy(manifest)
    if not retain:
        stage, backup = _retention_paths(directory)
        stage.mkdir()
        for call in calls:
            call["response_path"] = None
            call["response_evidence_retained"] = False
        for part in manifest["participants"]:
            part["native_source_count"] = len(part["native_files"])
            part["native_files"] = []
        for relation in relations:
            for evidence in relation.get("evidence", []):
                evidence["source"] = evidence.pop("path")
                evidence["retained"] = False
        write_jsonl(stage / "calls.jsonl", calls)
        write_jsonl(stage / "relations.jsonl", relations)
    else:
        stage = directory
    manifest.update(storage_layout="compact/v2", debug_retention=mode, raw_retained=retain)
    manifest["files"] = [{"path": p.relative_to(stage).as_posix(),
                          "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                         for p in sorted(stage.rglob("*")) if p.is_file() and p.name != "manifest.json"]
    atomic_write_json(stage / "manifest.json", manifest)
    validate_capture(stage, {"manifest": "manifest.json", "capture_status": manifest["capture_status"]})
    if not retain:
        directory.rename(backup)
        try:
            stage.rename(directory)
            validate_capture(directory, {"manifest": "manifest.json", "capture_status": manifest["capture_status"]})
        except Exception:
            recover_capture_retention(directory, prefer_raw=True)
            raise
        try:
            shutil.rmtree(backup)
        except OSError as exc:
            # The newly published attempt is valid; stale backup cleanup can
            # safely be retried by recover_capture_retention on the next run.
            logger.warning("capture retention backup cleanup deferred: %s", exc)
    validate_capture(directory, {"manifest": "manifest.json", "capture_status": manifest["capture_status"]})
    return manifest
