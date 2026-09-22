"""Conservative training diagnostics; never rewrite requests or trajectory text."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from roller.integrity import trajectory_conversion_unavailable
from roller.thinking import THINKING_LEVELS


def sft_exclusion_reason(result: dict[str, Any]) -> str | None:
    """Single policy shared by dataset export and reporting, including resume."""
    if result.get("status") not in {"ok", "skipped"}:
        return "execution_failed"
    runtime = result.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    eligibility = runtime.get("sft_eligibility")
    if isinstance(eligibility, dict) and eligibility.get("eligible") is False:
        reason = eligibility.get("reason")
        return reason if isinstance(reason, str) and reason else "ineligible"
    # Older samples have a thinking audit but no explicit eligibility field.
    audit = runtime.get("thinking_audit")
    if isinstance(audit, dict) and audit.get("status") == "mismatch":
        return "thinking_effort_mismatch"
    if trajectory_conversion_unavailable(runtime) in {"actf", "sft"}:
        return "conversion_unavailable"
    return None


def training_summary(results: list[dict[str, Any]]) -> dict[str, int]:
    """Count finalized primary-agent samples, not requests or simulators.

    Audit categories overlap: a sample may have visible-reasoning mismatch
    and unverified wire controls. Missing legacy audits are not counted as
    matched or not-observable; no new evidence is inferred on resume.
    """
    summary = dict(sft_included=0, sft_excluded=0, thinking_mismatch=0,
                   wire_unverified=0, wire_not_observable=0)
    for result in results:
        key = "sft_excluded" if sft_exclusion_reason(result) else "sft_included"
        summary[key] += 1
        runtime = result.get("runtime")
        runtime = runtime if isinstance(runtime, dict) else {}
        wire = runtime.get("wire_effort_audit")
        visible = runtime.get("thinking_audit")
        wire_status = wire.get("status") if isinstance(wire, dict) else None
        visible_status = visible.get("status") if isinstance(visible, dict) else None
        if wire_status == "mismatch" or visible_status == "mismatch":
            summary["thinking_mismatch"] += 1
        if wire_status == "unverified":
            summary["wire_unverified"] += 1
        elif wire_status == "not_observable":
            summary["wire_not_observable"] += 1
    return summary


def wire_effort_audit(path: Path, requested: str | None) -> dict[str, Any] | None:
    if requested is None:
        return None
    requests = []
    unreadable = False
    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                try:
                    event = json.loads(line)
                except ValueError:
                    unreadable = True
                    continue
                if not isinstance(event, dict):
                    continue
                endpoint = str(event.get("path", "")).split("?", 1)[0].rstrip("/")
                if not endpoint.endswith(("/chat/completions", "/responses", "/messages")):
                    continue
                body = event.get("body", event.get("params"))
                if not isinstance(body, dict):
                    unreadable = True
                    continue
                efforts = []
                if "reasoning_effort" in body:
                    efforts.append(body["reasoning_effort"])
                for key in ("reasoning", "output_config"):
                    value = body.get(key)
                    if isinstance(value, dict) and "effort" in value:
                        efforts.append(value["effort"])
                efforts = ["off" if e == "none" else e for e in efforts]
                thinking = body.get("thinking")
                mode = thinking.get("type") if isinstance(thinking, dict) else None
                enabled = body.get("enable_thinking")
                template = body.get("chat_template_kwargs")
                switches = [enabled]
                if isinstance(template, dict):
                    switches.append(template.get("enable_thinking"))
                disabled = mode == "disabled" or any(v is False for v in switches)
                active = mode in ("enabled", "adaptive") or any(v is True for v in switches)
                known = [e for e in efforts if isinstance(e, str) and e in THINKING_LEVELS]
                mismatch = any(e != requested for e in known)
                mismatch |= (requested == "off" and active) or (requested != "off" and disabled)
                if mismatch:
                    status = "mismatch"
                elif efforts and len(known) == len(efforts):
                    status = "matched"
                elif requested == "off" and disabled:
                    status = "matched"
                else:
                    status = "unverified"  # omission/budget/enabled does not prove an exact effort
                requests.append({
                    "seq": event.get("seq"), "status": status,
                    "observed_efforts": known,
                    "enabled": active, "disabled": disabled,
                    "unrecognized_effort": len(known) != len(efforts),
                })
    except (OSError, UnicodeError):
        unreadable = True
    status = "not_observable"
    if any(r["status"] == "mismatch" for r in requests):
        status = "mismatch"
    elif requests:
        status = "matched" if not unreadable and all(r["status"] == "matched" for r in requests) else "unverified"
    return {"requested_effort": requested, "status": status, "requests": requests}


def completion_audit(trajectory: dict[str, Any]) -> dict[str, Any]:
    steps = trajectory.get("steps")
    if not isinstance(steps, list) or not steps or not isinstance(steps[-1], dict):
        return {"status": "not_observable"}
    last = steps[-1]
    metric = last.get("metric") or {}
    reason = metric.get("stop_reason") if isinstance(metric, dict) else None
    if reason in ("length", "max_tokens", "max_output_tokens"):
        return {"status": "incomplete", "reason": "token_limit", "stop_reason": reason}
    assistant = last.get("assistant_content") or {}
    content = assistant.get("content") if isinstance(assistant, dict) else None
    if isinstance(content, str) and not content.strip() and not assistant.get("tool_calls"):
        return {"status": "incomplete", "reason": "empty_final_answer"}
    return {"status": "not_flagged"}  # Not a task grader or proof of completeness.


def native_completion_audit(path: Path, schema: str | None) -> dict[str, Any]:
    """Read completed native assistant messages when strict ACTF conversion fails.

    Deliberately ignore request history, partial deltas, error prose and unknown
    schemas. This records evidence without weakening the converter's contract.
    """
    if schema not in ("roller.pi-events/v1", "openclaw-session-jsonl"):
        return {"status": "not_observable", "source": "native"}
    last = None
    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                try:
                    row = json.loads(line)
                except ValueError:
                    return {"status": "not_observable", "source": "native"}
                if not isinstance(row, dict):
                    continue
                if schema == "roller.pi-events/v1":
                    event = row.get("event")
                    if row.get("type") != "event" or not isinstance(event, dict) or event.get("type") != "message_end":
                        continue
                    message = event.get("message")
                else:
                    message = row.get("message") if row.get("type") == "message" else None
                if isinstance(message, dict) and message.get("role") == "assistant":
                    last = message
    except (OSError, UnicodeError):
        last = None
    if last is None:
        return {"status": "not_observable", "source": "native"}
    content = last.get("content")
    calls = []
    if isinstance(content, list):
        calls = [b for b in content if isinstance(b, dict) and b.get("type") == "toolCall"]
        if all(isinstance(b, dict) and b.get("type") in ("text", "thinking", "toolCall") for b in content):
            text = [b.get("text") for b in content if b.get("type") == "text"]
            content = "".join(text) if all(isinstance(t, str) for t in text) else None
        else:
            content = None
    audit = completion_audit({"steps": [{
        "assistant_content": {"content": content, "tool_calls": calls},
        "metric": {"stop_reason": last.get("stopReason", last.get("stop_reason"))},
    }]})
    return {**audit, "source": "native"}


def model_identity_audit(path: Path) -> dict[str, Any]:
    requests: dict[int, dict[str, Any]] = {}
    responses = []
    unreadable = False
    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                try:
                    row = json.loads(line)
                except ValueError:
                    unreadable = True
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("kind") == "response_metadata":
                    responses.append(row)
                elif isinstance(row.get("seq"), int) and isinstance(row.get("model"), str):
                    requests[row["seq"]] = {
                        "request_seq": row["seq"], "requested_model": row["model"],
                        "response_models": [], "finish_reasons": [], "status": "not_observable",
                        "response_statuses": [],
                    }
    except (OSError, UnicodeError):
        unreadable = True
    for response in responses:
        seq = response.get("request_seq")
        request = requests.get(seq) if isinstance(seq, int) else None
        if request is None:
            continue
        models = response.get("response_models")
        if not isinstance(models, list) or any(not isinstance(m, str) for m in models):
            continue
        request["response_models"] = models
        request["finish_reasons"] = response.get("finish_reasons", [])
        request["response_statuses"] = response.get("response_statuses", [])
        request["status"] = (
            "name_mismatch" if any(m != request["requested_model"] for m in models)
            else "matched" if models and response.get("capture_status") == "observed"
            else "not_observable"
        )
    statuses = [r["status"] for r in requests.values()]
    status = "name_mismatch" if "name_mismatch" in statuses else "matched" if not unreadable and statuses and all(s == "matched" for s in statuses) else "not_observable"
    return {"status": status, "requests": list(requests.values())}


def record_training_audits(metadata: dict[str, Any], trajectory: dict[str, Any] | None, wire_path: Path) -> None:
    settings = metadata.get("settings")
    settings = settings if isinstance(settings, dict) else {}
    effort = metadata.get("reasoning_effort", settings.get("reasoning_effort"))
    wire = wire_effort_audit(wire_path, effort)
    identity = model_identity_audit(wire_path)
    metadata["model_identity_audit"] = identity
    if identity["status"] == "name_mismatch":
        warning = "Response model name differs from the wire request; see model_identity_audit (aliases may be legitimate)."
        warnings = metadata.get("warnings")
        if not isinstance(warnings, list):
            warnings = metadata["warnings"] = []
        if warning not in warnings:
            warnings.append(warning)
            logging.getLogger(__name__).warning(warning)
    reasons = []
    if wire is not None:
        metadata["wire_effort_audit"] = wire
        if wire["status"] == "mismatch":
            reasons.append("wire_effort_mismatch")
    if trajectory is not None:
        completion = completion_audit(trajectory)
    else:
        completion = native_completion_audit(
            wire_path.with_name("trace.jsonl"), metadata.get("raw_trace_schema"),
        )
    metadata["completion_audit"] = completion
    if completion["status"] == "incomplete":
        reasons.append(completion["reason"])
    if reasons:
        existing = metadata.get("sft_eligibility")
        if isinstance(existing, dict) and existing.get("eligible") is False and existing.get("reason"):
            reasons.insert(0, existing["reason"])
        metadata["sft_eligibility"] = {"eligible": False, "reason": reasons[0], "reasons": list(dict.fromkeys(reasons))}
