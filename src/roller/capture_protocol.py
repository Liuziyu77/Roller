"""Harness-independent call attribution, relations and artifact publication.

Adapters supply native evidence; this module owns recording/export decisions.
The v1 on-disk schemas remain compatible with the original Claude capture.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import shutil
import gzip
import zlib
from pathlib import Path
from typing import Any

from roller.capture import read_jsonl, _safe_file, apply_capture_retention
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
                     recorder_error: str | None = None, debug_retention: str = "full") -> dict[str, Any]:
    event_path = directory / "events.jsonl"
    if not event_path.exists():
        event_path = directory / "raw/events.jsonl"
    if not event_path.exists() and (directory / "manifest.json").exists():
        raise ValueError("capture has no raw events; cannot re-finalize a lean capture")
    events = read_jsonl(event_path) if event_path.exists() else []
    contexts = None
    for event in events:
        if event.get("type") == "request" and "body_ref" in event:
            if contexts is None:
                contexts = {c["call_id"]: c["context"] for c in read_jsonl(directory / "calls.jsonl")}
            event["body"] = contexts[event["body_ref"]["call_id"]]
    requests = {e["request_seq"]: e for e in events if e.get("type") == "request"}
    responses = {e["request_seq"]: e for e in events if e.get("type") == "response"}
    calls = []
    for seq, event in sorted(requests.items()):
        endpoint = event["path"].split("?", 1)[0].rstrip("/")
        protocol = next((value for suffix, value in {
            "/messages": "anthropic-messages", "/chat/completions": "openai-chat-completions",
            "/responses": "openai-responses",
        }.items() if endpoint.endswith(suffix)), native.protocol)
        completion = responses.get(seq, {})
        output = None
        errors = []
        if not completion.get("complete") or completion.get("error"):
            errors.append(completion.get("error") or "response capture incomplete")
        response_path = completion.get("response_path")
        try:
            if not response_path:
                raise ValueError("response is missing")
            payload = _safe_file(directory, response_path).read_bytes()
            encoding = completion.get("content_encoding", "").lower()
            if encoding == "gzip":
                payload = gzip.decompress(payload)
            elif encoding == "deflate":
                payload = zlib.decompress(payload)
            elif encoding not in {"", "identity"}:
                raise ValueError("unsupported response content encoding")
            output = decode_response(protocol, payload, completion.get("content_type", ""))
        except (OSError, ValueError, KeyError, TypeError, zlib.error) as exc:
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
        finished = bool(output and response_finished(protocol, output))
        if completion.get("http_status") != 200:
            errors.append("model HTTP request did not succeed")
        if not finished:
            errors.append("model response did not finish normally")
        if output and output.get("model") != event["body"].get("model"):
            errors.append("response model does not match request")
        call = {"schema_version": "roller.call/v1", "call_id": event["call_id"],
            "attempt_id": directory.name, "request_seq": seq, "participant_id": pid,
            "protocol": protocol, "output_source": "model",
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
        context_message = relation.pop("context_message", None)
        parent_mid = relation.pop("parent_native_message_id", None)
        parent = relation.get("parent_participant_id")
        tool_id = relation.get("tool_call_id")
        model_ref = relation.get("model_tool_ref")
        def matches_parent(call):
            if model_ref:
                return call["protocol"] == model_ref["protocol"] and any(
                    item.get("call_id") == model_ref["call_id"] and item.get("id") == model_ref["item_id"]
                    for item in call["output"].get("output", []) if item.get("type") == "function_call")
            return (parent_mid and call["output"].get("id") == parent_mid) or tool_id in tool_call_ids(call["protocol"], call["output"])
        candidates = [c for c in calls if c["participant_id"] == parent and c["output"] and matches_parent(c)]
        relation["parent_call_id"] = candidates[0]["call_id"] if len(candidates) == 1 else None
        if len(candidates) != 1:
            relation["status"] = "unknown"
            relation["reason"] = "native relation has no unique captured parent call"
        relations.append(relation)
        if relation["type"] not in {"result_delivered", "launch_acknowledged"}:
            continue
        for call in calls:
            if relation.get("delivery_mode") == "notification" or context_message is not None:
                texts = [m.get("content") for m in call["context"].get("input" if call["protocol"] == "openai-responses" else "messages", []) if m.get("role") == "user"]
                texts = [t if isinstance(t, str) else "\n".join(b.get("text", "") for b in t if isinstance(b, dict))
                         for t in texts if isinstance(t, (str, list))]
                in_context = bool(context_message) and any(context_message == t for t in texts)
            else:
                result_ids = tool_result_ids(call["protocol"], call["context"])
                reference_ids = {relation.get("context_tool_call_id", tool_id)}
                if model_ref and call["protocol"] == model_ref["protocol"]:
                    reference_ids.add(model_ref["call_id"])
                in_context = bool(reference_ids & result_ids)
            if call["participant_id"] == parent and in_context:
                relations.append({**relation,
                    "type": "result_in_context" if relation["type"] == "result_delivered" else "launch_ack_in_context",
                    "call_id": call["call_id"]})
    relation_scope = calls[0]["call_id"].rsplit(":call-", 1)[0] if calls else directory.name
    for index, relation in enumerate(relations):
        relation["schema_version"] = "roller.relation/v1"
        relation["relation_id"] = f"{relation_scope}:relation-{index:08d}"

    for pid, part in native.participants.items():
        owned = [c for c in calls if c["participant_id"] == pid]
        part["call_ids"] = [c["call_id"] for c in owned]
        part.pop("calls_path", None)
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
    _compact_artifacts(directory, calls, relations, native.participants, events)
    files = [{"path": p.relative_to(directory).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
             for p in sorted(directory.rglob("*")) if p.is_file() and p.name != "manifest.json"]
    complete = bool(calls) and not recorder_error and not native.errors and not native.background_detected and all(c["capture_status"] == "complete" for c in calls) and all(p["capture_status"] == "complete" for p in native.participants.values()) and all(r["status"] == "observed" for r in relations)
    manifest = {"schema_version": "roller.capture/v1", "attempt_id": directory.name,
        "harness": native.harness, "storage_layout": "compact/v1", "execution_status": execution_status,
        "capture_status": "complete" if complete else "partial", "recorder_error": recorder_error,
        "native_errors": native.errors,
        "participants": list(native.participants.values()), "background_detected": native.background_detected,
        "call_count": len(calls), "relation_count": len(relations), "files": files,
        "capabilities": native.capabilities,
        "coverage": {"request_response": "observed HTTP calls only",
                     "identity": native.capabilities.get("identity", "unknown"),
                     "lifecycle": "native registry completion and announcement drain" if native.capabilities.get("background_drain")
                                  else "foreground native evidence; background drain not supported"}}
    atomic_write_json(directory / "manifest.json", manifest)
    return apply_capture_retention(directory, manifest, debug_retention)


def _compact_artifacts(directory: Path, calls: list[dict[str, Any]],
                       relations: list[dict[str, Any]], participants: dict[str, dict[str, Any]],
                       events: list[dict[str, Any]]) -> None:
    """Publish one copy of each context and group source evidence under raw/."""
    raw = directory / "raw"
    raw.mkdir(exist_ok=True)
    def relocated(path):
        if path and (path.startswith("native/") or path.startswith("responses/")):
            return "raw/" + path
        return path

    for name in ("native", "responses", "native_index.json"):
        source = directory / name
        if source.exists():
            target = raw / name
            if target.exists():
                raise ValueError(f"capture relocation target already exists: {target}")
            source.rename(target)
    for call in calls:
        call["response_path"] = relocated(call.get("response_path"))
    for part in participants.values():
        part["native_files"] = [relocated(p) for p in part["native_files"]]
    for relation in relations:
        for evidence in relation.get("evidence", []):
            evidence["path"] = relocated(evidence.get("path"))
    for event in events:
        if event.get("type") == "request":
            event.pop("body", None)
            event["body_ref"] = {"path": "calls.jsonl", "call_id": event["call_id"], "field": "context"}
        if "response_path" in event:
            event["response_path"] = relocated(event["response_path"])
    index = raw / "native_index.json"
    if index.exists():
        rows = json.loads(index.read_text())
        for row in rows:
            row["path"] = relocated(row["path"])
        atomic_write_json(index, rows)
    write_jsonl(directory / "calls.jsonl", calls)
    write_jsonl(directory / "relations.jsonl", relations)
    write_jsonl(raw / "events.jsonl", events)
    (directory / "events.jsonl").unlink(missing_ok=True)
    # Remove obsolete derived copies when compacting a legacy attempt.
    if (directory / "participants").exists():
        shutil.rmtree(directory / "participants")
