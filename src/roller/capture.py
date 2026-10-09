"""HTTP call recording and capture artifact integrity.

The HTTP recorder never guesses an agent from timing or prompt text. Attribution
is resolved after collection using response IDs and native assistant IDs.
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

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

    def begin(self, seq: int, path: str, body: dict[str, Any]) -> str:
        call_id = f"{self.scope_id}:{self.directory.name}:call-{seq:08d}"
        with self._lock:
            if self.sealed:
                return call_id
            self.request_seqs.add(seq)
            self.write({"type": "request", "call_id": call_id,
                        "request_seq": seq, "path": path, "body": body})
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
    participants = manifest.get("participants", [])
    pids = {part["participant_id"] for part in participants}
    if len(pids) != len(participants):
        raise ValueError("duplicate capture participant IDs")
    for part in participants:
        if part.get("calls_path") not in inventory or any(p not in inventory for p in part.get("native_files", [])):
            raise ValueError("participant references unregistered capture files")
        actual = read_jsonl(_safe_file(path.parent, part["calls_path"]))
        expected = [c for c in calls if c.get("participant_id") == part["participant_id"]]
        if actual != expected:
            raise ValueError("participant calls do not match capture calls")
    for call in calls:
        if call.get("schema_version") != "roller.call/v1" or not isinstance(call.get("context"), dict):
            raise ValueError("invalid capture call schema")
        if call.get("participant_id") is not None and call["participant_id"] not in pids:
            raise ValueError("call participant is missing")
        if call.get("capture_status") == "complete":
            if call.get("response_path") not in inventory or not isinstance(call.get("output"), dict):
                raise ValueError("complete call lacks response evidence")
    relations = read_jsonl(path.parent / "relations.jsonl")
    if len(relations) != manifest.get("relation_count"):
        raise ValueError("capture relation count mismatch")
    for relation in relations:
        if relation.get("schema_version") != "roller.relation/v1":
            raise ValueError("invalid capture relation schema")
        for key in ("parent_call_id", "call_id"):
            if relation.get(key) is not None and relation[key] not in ids:
                raise ValueError("relation call reference is missing")
        for key in ("parent_participant_id", "child_participant_id"):
            if relation.get(key) is not None and relation[key] not in pids:
                raise ValueError("relation participant is missing")
        for evidence in relation.get("evidence", []):
            if evidence.get("path") not in inventory:
                raise ValueError("relation evidence file is missing")


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
