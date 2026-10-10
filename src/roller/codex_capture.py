"""Codex thread identity and delegation evidence from native rollout JSONL."""
import json
from pathlib import Path

from roller.capture import read_jsonl
from roller.codex_tools import read_tool_capture
from roller.capture_protocol import NativeCapture
from roller.storage import write_jsonl


def retain_root_tools(capture, trace: Path) -> None:
    """Keep all snapshots as evidence, expose only root schemas to main SFT."""
    path = capture.output_dir / "tools_capture.jsonl"
    if not path.exists():
        return
    capture.add_native(path, "all-tools.jsonl")
    meta = next((r["payload"] for r in read_jsonl(trace) if r.get("type") == "session_meta"), {})
    events = read_jsonl(capture.recorder.events)
    seqs = {r["request_seq"] for r in events if r.get("type") == "request"
            and r.get("native_identity", {}).get("session_id") == meta.get("id")}
    original_capture = read_tool_capture(path)
    rows = read_jsonl(path)
    snapshots = [r for r in rows if r.get("kind") == "codex_tools" and r.get("seq") in seqs]
    errors = [r for r in rows if r.get("kind") == "codex_tools_error"]
    if original_capture["status"] == "invalid":
        errors.append({"kind": "codex_tools_error", "error": original_capture["error"]})
    complete = original_capture["status"] != "invalid" and any(r.get("kind") == "codex_tools_complete" for r in rows)
    footer = [{"kind": "codex_tools_complete", "request_count": len(seqs)}] if complete else []
    write_jsonl(path, [*snapshots, *errors, *footer])


def read_codex_native(directory: Path) -> NativeCapture:
    native = NativeCapture("codex", "openai-responses", capabilities={
        "request_response": "full", "identity": "native_thread_header",
        "native_output_coverage": "token_usage_updates", "subagents": "native_spawn_agent",
        "background_drain": False,
    })
    for path in sorted((directory / "native").rglob("*.jsonl")):
        rows = read_jsonl(path)
        meta = next((r.get("payload", {}) for r in rows if r.get("type") == "session_meta"), {})
        sid = meta.get("id")
        if not isinstance(sid, str):
            continue
        source = path.relative_to(directory).as_posix()
        parent = meta.get("parent_thread_id")
        pid = "thread:" + sid
        native.participants[pid] = {"participant_id": pid, "native_session_id": sid,
            "role": "child" if parent else "root", "identity_status": "observed",
            "native_files": [source], "call_ids": []}
        native.session_owners.setdefault(sid, set()).add(pid)
        totals = set()
        tools = {}
        for row in rows:
            item = row.get("payload", {})
            if row.get("type") == "event_msg" and item.get("type") == "token_count":
                usage = (item.get("info") or {}).get("total_token_usage")
                if isinstance(usage, dict):
                    totals.add(json.dumps(usage, sort_keys=True))
            if row.get("type") != "response_item":
                continue
            if item.get("type") == "function_call":
                tools[item.get("call_id")] = item.get("name")
            if item.get("type") != "function_call_output":
                continue
            call_id = item.get("call_id")
            output = item.get("output")
            if isinstance(output, dict):
                output = output.get("content")
            try:
                result = json.loads(output) if isinstance(output, str) else output
            except ValueError:
                continue
            if not isinstance(result, dict):
                continue
            evidence = [{"path": source, "event_id": call_id}]
            base = {"parent_participant_id": pid, "tool_call_id": call_id,
                    "status": "observed", "evidence": evidence}
            if tools.get(call_id) == "spawn_agent" and isinstance(result.get("agent_id"), str):
                child = "thread:" + result["agent_id"]
                native.relations.extend([{**base, "child_participant_id": child, "type": kind}
                    for kind in ("spawn", "launch_acknowledged")])
            elif tools.get(call_id) in {"wait", "wait_agent"}:
                for child_id, status in result.get("status", {}).items():
                    if isinstance(status, dict) and ("completed" in status or "errored" in status):
                        native.relations.extend([{**base, "child_participant_id": "thread:" + child_id, "type": kind}
                            for kind in ("child_completed" if "completed" in status else "child_failed", "result_delivered")])
        native.expected_call_counts[pid] = len(totals)
    known = set(native.participants)
    for relation in native.relations:
        if relation["child_participant_id"] not in known:
            relation["status"] = "unknown"
            relation["reason"] = "spawned thread has no collected native rollout"
            # Preserve evidence without a dangling participant reference.
            relation["native_child_thread_id"] = relation.pop("child_participant_id")
            relation["child_participant_id"] = None
    started = {r.get("child_participant_id") for r in native.relations if r["type"] == "spawn"}
    ended = {r.get("child_participant_id") for r in native.relations if r["type"] in {"child_completed", "child_failed"}}
    native.background_detected = bool(started - ended)
    return native
