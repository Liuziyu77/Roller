"""Harness-independent call attribution, relations and artifact publication.

Adapters supply native evidence; this module owns recording/export decisions.
The v1 on-disk schemas remain compatible with the original Claude capture.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Any

from roller.capture import read_jsonl
from roller.model_protocols import decode_response, response_finished, tool_call_ids, tool_result_ids
from roller.storage import atomic_write_json, write_jsonl


@dataclass
class NativeCapture:
    """Evidence returned by a harness adapter, without export policy.

response_owners matches provider output IDs; session_owners matches a native
session ID recorded alongside the HTTP request. Neither uses prompt or time.
Expected IDs/counts describe observable native assistant outputs, not retries.
Relations carry file/event evidence and optionally a parent native message ID.
"""

    harness: str
    protocol: str
    participants: dict[str, dict[str, Any]] = field(default_factory=dict)
    response_owners: dict[str, set[str]] = field(default_factory=dict)
    session_owners: dict[str, set[str]] = field(default_factory=dict)
    expected_call_counts: dict[str, int] = field(default_factory=dict)
    relations: list[dict[str, Any]] = field(default_factory=list)
    background_detected: bool = False
    capabilities: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def finalize_capture(directory: Path, native: NativeCapture, *, execution_status: str,
                     recorder_error: str | None = None) -> dict[str, Any]:
    events = read_jsonl(directory / "events.jsonl") if (directory / "events.jsonl").exists() else []
    requests = {e["request_seq"]: e for e in events if e.get("type") == "request"}
    responses = {e["request_seq"]: e for e in events if e.get("type") == "response"}
    calls = []
    for seq, event in sorted(requests.items()):
        completion = responses.get(seq, {})
        output = None
        errors = []
        if not completion.get("complete") or completion.get("error"):
            errors.append(completion.get("error") or "response capture incomplete")
        response_path = completion.get("response_path")
        try:
            if completion.get("content_encoding") not in {"", "identity"}:
                raise ValueError("encoded response is preserved raw but not parsed")
            if not response_path:
                raise ValueError("response is missing")
            output = decode_response(native.protocol, (directory / response_path).read_bytes(), completion.get("content_type", ""))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(str(exc))
        if output is not None and (not isinstance(output.get("id"), str) or not output["id"]):
            errors.append("model response has no valid response ID")
        identity = event.get("native_identity", {})
        session_id = identity.get("session_id")
        if session_id:
            matches = native.session_owners.get(session_id, set())
        else:
            matches = native.response_owners.get(output.get("id"), set()) if output else set()
        pid = next(iter(matches)) if len(matches) == 1 else None
        identity_confirmed = pid in native.participants and native.participants[pid].get("identity_status") == "observed"
        if pid is None:
            errors.append("call has no unique native participant")
        elif not identity_confirmed:
            errors.append("native participant identity is unresolved")
        finished = bool(output and response_finished(native.protocol, output))
        if completion.get("http_status") != 200:
            errors.append("model HTTP request did not succeed")
        if not finished:
            errors.append("model response did not finish normally")
        if output and output.get("model") != event["body"].get("model"):
            errors.append("response model does not match request")
        call = {"schema_version": "roller.call/v1", "call_id": event["call_id"],
            "attempt_id": directory.name, "request_seq": seq, "participant_id": pid,
            "protocol": native.protocol, "output_source": "model",
            "started_at": event.get("timestamp"), "finished_at": completion.get("timestamp"),
            "request_path": event["path"], "context": event["body"], "output": output,
            "http_status": completion.get("http_status"), "response_path": response_path,
            "response_capture_status": "complete" if completion.get("complete") and not completion.get("error") else "partial",
            "identity_status": "observed" if identity_confirmed else "unknown",
            "identity_basis": "native_session" if session_id else "response_message_id",
            "execution_status": "completed" if finished and completion.get("http_status") == 200 else "failed",
            "capture_status": "complete" if not errors else "partial",
            "export_status": "available" if not errors else "unavailable", "errors": errors}
        calls.append(call)

    relations = []
    for observation in native.relations:
        relation = dict(observation)
        parent_mid = relation.pop("parent_native_message_id", None)
        parent = relation.get("parent_participant_id")
        tool_id = relation.get("tool_call_id")
        candidates = [c for c in calls if c["participant_id"] == parent and c["output"]
                      and ((parent_mid and c["output"].get("id") == parent_mid)
                           or tool_id in tool_call_ids(native.protocol, c["output"]))]
        relation["parent_call_id"] = candidates[0]["call_id"] if len(candidates) == 1 else None
        if len(candidates) != 1:
            relation["status"] = "unknown"
            relation["reason"] = "native relation has no unique captured parent call"
        relations.append(relation)
        if relation["type"] not in {"result_delivered", "launch_acknowledged"}:
            continue
        for call in calls:
            if call["participant_id"] == parent and tool_id in tool_result_ids(native.protocol, call["context"]):
                relations.append({**relation,
                    "type": "result_in_context" if relation["type"] == "result_delivered" else "launch_ack_in_context",
                    "call_id": call["call_id"]})
    relation_scope = calls[0]["call_id"].rsplit(":call-", 1)[0] if calls else directory.name
    for index, relation in enumerate(relations):
        relation["schema_version"] = "roller.relation/v1"
        relation["relation_id"] = f"{relation_scope}:relation-{index:08d}"

    write_jsonl(directory / "calls.jsonl", calls)
    write_jsonl(directory / "relations.jsonl", relations)
    for pid, part in native.participants.items():
        owned = [c for c in calls if c["participant_id"] == pid]
        relative = "participants/" + hashlib.sha256(pid.encode()).hexdigest()[:16] + "/calls.jsonl"
        write_jsonl(directory / relative, owned)
        part["call_ids"] = [c["call_id"] for c in owned]
        part["calls_path"] = relative
        expected = {mid for mid, owners in native.response_owners.items() if pid in owners}
        captured = {c["output"]["id"] for c in owned if c["output"] and c["capture_status"] == "complete"}
        part["uncaptured_message_ids"] = sorted(expected - captured)
        expected_count = native.expected_call_counts.get(pid)
        valid_count = sum(c["capture_status"] == "complete" for c in owned)
        count_ok = expected_count is None or expected_count == valid_count
        if expected_count is not None:
            part["native_completed_call_count"] = expected_count
            part["captured_complete_call_count"] = valid_count
        part["capture_status"] = "complete" if part["native_files"] and (expected or expected_count) and not (expected - captured) and count_ok else "partial"
    files = [{"path": p.relative_to(directory).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
             for p in sorted(directory.rglob("*")) if p.is_file() and p.name != "manifest.json"]
    complete = bool(calls) and not recorder_error and not native.errors and not native.background_detected and all(c["capture_status"] == "complete" for c in calls) and all(p["capture_status"] == "complete" for p in native.participants.values()) and all(r["status"] == "observed" for r in relations)
    manifest = {"schema_version": "roller.capture/v1", "attempt_id": directory.name,
        "harness": native.harness, "execution_status": execution_status,
        "capture_status": "complete" if complete else "partial", "recorder_error": recorder_error,
        "native_errors": native.errors,
        "participants": list(native.participants.values()), "background_detected": native.background_detected,
        "call_count": len(calls), "relation_count": len(relations), "files": files,
        "capabilities": native.capabilities,
        "coverage": {"request_response": "observed HTTP calls only",
                     "identity": native.capabilities.get("identity", "unknown"),
                     "lifecycle": "foreground native evidence; background drain not supported"}}
    atomic_write_json(directory / "manifest.json", manifest)
    return manifest
