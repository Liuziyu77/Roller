"""Evidence-based side-branch acceptance of the upstream-compatible SFT export.

The caller supplies a frozen task contract; file names are never evidence of
which image the model saw. Historical verdicts and sample/model names cannot
grant qualification. No collected script is executed here.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
from typing import Any

from roller.sft import _image_data_uri, actf_to_sft, validate_sft_record
from roller.storage import atomic_write_json
from roller_chart2code.png_check import inspect_png
from roller_chart2code.profiles import freeze_profile
from roller_chart2code.render_journal import _safe, validate_render_journal

VERSION = "roller.chart2code-acceptance/v3"
MAX_JSON_BYTES = 128 * 1024 * 1024
MAIN_COMMIT = "7cd8a563a83c1e930a3fec7d9dad47272328af8d"


def file_hash(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("evidence must be a regular, non-symlink file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_object(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("unsafe or oversized JSON artifact")
    with path.open("rb") as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("JSON artifact grew past the read limit")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("expected JSON object")
    return result


def safe_root(path: Path) -> Path:
    path = Path(path).expanduser().absolute()
    if any(c in str(path) for c in "\r\n\0") or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("unsafe root path or symlink parent")
    return path.resolve()


def valid_jsonl(path: Path) -> bool:
    """Stream the entire artifact, not just the first parseable event."""
    if not path.is_file() or path.is_symlink():
        return False
    seen = False
    with path.open("rb") as stream:
        while line := stream.readline(MAX_JSON_BYTES + 1):
            if len(line) > MAX_JSON_BYTES:
                return False
            if not line.strip():
                continue
            if not isinstance(json.loads(line), dict):
                return False
            seen = True
    return seen


def content_hashes(content):
    """Only explicit image content blocks; do not inspect arguments/reasoning."""
    import base64
    if not isinstance(content, list):
        return []
    hashes = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in {"image", "image_url", "input_image"}:
            uri = _image_data_uri(block, "task image evidence")
            hashes.append(hashlib.sha256(base64.b64decode(uri.partition(",")[2], validate=True)).hexdigest())
    return hashes


def evidence_inventory(sample: Path) -> dict[str, str]:
    paths = {sample / name for name in ("info.json", "trace.jsonl", "trace_ACTF.json", "trace_SFT.json", "wire_trace.jsonl")}
    results = sample / "results"
    if results.is_symlink():
        raise ValueError("results must not be a symlink")
    if results.is_dir():
        paths.update(path for path in results.rglob("*") if not path.is_dir() or path.is_symlink())
    hashes = {}
    for path in sorted(paths):
        relative = path.relative_to(sample).as_posix()
        safe = _safe(sample, relative)
        if safe.is_file():
            hashes[relative] = file_hash(safe)
        elif safe.exists():
            raise ValueError("non-regular evidence artifact")
    return hashes


def implementation_identity() -> dict:
    """Actual source hashes also distinguish uncommitted/installed validators."""
    root = Path(__file__).resolve().parent
    source_hashes = {p.name: file_hash(p) for p in sorted(root.glob("*.py"))}
    commit = None
    # Do not accidentally report an unrelated enclosing repository when installed.
    if (root.parent.parent / ".git").exists():
        try:
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                                    capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return {"main_commit": MAIN_COMMIT, "chart2code_commit": commit, "source_sha256": source_hashes}


def object_field(value, field):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def audit_sample(sample: Path, contract: dict[str, Any], *, report_only: bool = False) -> dict:
    sample = safe_root(sample)
    expected_profile = freeze_profile(contract["profile"]["name"], contract["profile"]["reference"],
                                      max_renders=contract["profile"]["max_render_attempts"])
    if contract["profile"] != expected_profile:
        raise ValueError("task contract does not match the frozen profile")
    reference_hash = contract.get("reference_sha256", "")
    if not isinstance(reference_hash, str) or len(reference_hash) != 64 or any(c not in "0123456789abcdef" for c in reference_hash):
        raise ValueError("task contract needs a reference SHA256")
    gates = {name: {"passed": False, "checks": []} for name in ("infra", "artifact", "trajectory")}
    def check(gate, name, passed, detail=None):
        gates[gate]["checks"].append({"id": name, "passed": bool(passed), "detail": detail})
    info, actf, sft = {}, {}, {}
    viewed, references = [], []
    try:
        info = json_object(_safe(sample, "info.json"))
        identity = object_field(contract["identity"], "contract.identity")
        required = {"task_id", "model", "harness", "request_index", "sample_index", "sample_count", "source_index"}
        check("infra", "identity", required <= identity.keys() and all(info.get(k, {"sample_index": 0, "sample_count": 1}.get(k)) == v for k, v in identity.items()))
        check("infra", "execution", info.get("status") == "ok", info.get("status"))
        check("infra", "metadata_schema", info.get("schema_version") == "roller.sample/v1")
        runtime = object_field(info.get("runtime", {}), "runtime")
        check("infra", "cleanup", not runtime.get("retained_container", runtime.get("container")))
        check("infra", "raw_trace", valid_jsonl(_safe(sample, "trace.jsonl")))
        requests = object_field(info.get("usage", {}), "usage").get("request_count", 0)
        check("infra", "model_request", isinstance(requests, int) and not isinstance(requests, bool) and requests > 0)
        capture = object_field(contract.get("capture", {}), "contract.capture")
        if not isinstance(capture.get("require_wire_trace", False), bool):
            raise ValueError("require_wire_trace must be a boolean")
        if capture.get("require_wire_trace") or runtime.get("wire_trace") == "full":
            check("infra", "full_wire", valid_jsonl(_safe(sample, "wire_trace.jsonl")))
        check("trajectory", "generic_training", object_field(info.get("training", {}), "training").get("eligible") is True
              and object_field(info.get("conversion", {}), "conversion").get("status") == "ok")
        actf = json_object(_safe(sample, "trace_ACTF.json"))
        sft = json_object(_safe(sample, "trace_SFT.json"))
        validate_sft_record(sft)
        check("trajectory", "canonical_sft", actf_to_sft(actf) == sft)
        steps = actf.get("steps", [])
        check("trajectory", "actf_steps", actf.get("schema_version") == "ACTF_v1.0" and bool(steps)
              and all(step.get("step_id") == index for index, step in enumerate(steps, 1)))
        tool_successes = 0
        prompt_text = ""
        for step in steps:
            user = step.get("user_content")
            if user and not prompt_text:
                prompt_text = user if isinstance(user, str) else "\n".join(
                    block.get("text", "") for block in user if isinstance(block, dict) and block.get("type") in {"text", "input_text"})
            if reference_hash in content_hashes(user):
                references.append({"step_id": step["step_id"], "role": "user"})
            for observation in step.get("observation", []):
                if observation.get("isError"):
                    continue
                tool_successes += 1
                hashes = content_hashes(observation.get("content"))
                if reference_hash in hashes:
                    references.append({"step_id": step["step_id"], "role": "tool"})
                viewed.extend({"step_id": step["step_id"], "tool_call_id": observation.get("toolCallId"), "sha256": value} for value in hashes)
        check("infra", "tool_execution", tool_successes > 0)
        check("trajectory", "reference_delivery", bool(references),
              "Requires exact bytes in main-agent user/tool content; auxiliary calls alone are insufficient")
        # Task loader strips outer whitespace; it must not otherwise change the frozen prompt.
        check("trajectory", "frozen_prompt", prompt_text.strip() == expected_profile["prompt"].strip())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        check("trajectory", "readable_evidence", False, str(exc))
        viewed = []
    journal = None
    successes = []
    process_audit = {"reference_before_render_verified": False, "render_associations_verified": False,
                     "render_view_order_verified": False, "render_views": []}
    try:
        outputs = _safe(sample, "results")
        collection = object_field(info.get("output_collection", {}), "output_collection")
        check("artifact", "collection", collection.get("passed") is True)
        listed = set()
        for row in collection.get("files", []):
            object_field(row, "output_collection.files[]")
            path = _safe(outputs, row["path"])
            relative = path.relative_to(outputs).as_posix()
            if relative in listed:
                raise ValueError("duplicate output collection path")
            listed.add(relative)
            if not path.is_file() or path.stat().st_size != row["size"] or file_hash(path) != row["sha256"]:
                raise ValueError("output collection hash/size mismatch")
        actual = {p.relative_to(outputs).as_posix() for p in outputs.rglob("*") if not p.is_dir() or p.is_symlink()}
        check("artifact", "collection_complete", bool(listed) and actual == listed)
        journal = validate_render_journal(outputs, actf)
        check("artifact", "journal", journal is not None and journal["passed"], journal)
        check("artifact", "png", bool(inspect_png(_safe(outputs, "output.png"))))
        if journal:
            check("artifact", "budget", 1 <= journal["attempt_count"] <= expected_profile["max_render_attempts"])
            for attempt in journal["attempts"]:
                if attempt["status"] == "success":
                    metadata = json_object(_safe(outputs, attempt["metadata"]))
                    successes.append({"attempt_id": attempt["attempt_id"], "image_sha256": metadata["files"]["output.png"]["sha256"],
                                      "code_sha256": metadata["files"]["plot.py"]["sha256"], "render_tool_call": attempt["tool_call"]})
        links = [row.get("tool_call") for row in (journal or {}).get("attempts", [])]
        ordered_links = bool(links) and all(link and isinstance(link.get("step_id"), int) for link in links)
        if ordered_links:
            ordered_links = all(a["step_id"] <= b["step_id"] for a, b in zip(links, links[1:]))
        first_render = links[0]["step_id"] if ordered_links else None
        process_audit["reference_before_render_verified"] = any(
            (row["role"] == "user" and row["step_id"] == 1)
            or (first_render is not None and row["step_id"] < first_render)
            for row in references)
        process_audit["render_associations_verified"] = ordered_links
        if expected_profile["output_view_policy"] == "forbidden":
            output_hashes = {row["image_sha256"] for row in successes}
            check("trajectory", "single_render_no_output_view", not any(row["sha256"] in output_hashes
                  and not (row["sha256"] == reference_hash and first_render is not None and row["step_id"] < first_render)
                  for row in viewed))
        else:
            # Default acceptance requires actual rendered-image content in the
            # main trajectory, not a new marker absent from the legacy recorder.
            # Finer per-attempt ordering remains diagnostic, never guessed.
            rendered_hashes = {row["image_sha256"] for row in successes}
            observed_hashes = {row["sha256"] for row in viewed}
            check("trajectory", "rendered_image_coverage", bool(rendered_hashes) and rendered_hashes <= observed_hashes,
                  "Actual image bytes required; this does not certify a separate post-render view for every attempt")
            for success in successes:
                link = success["render_tool_call"]
                index = next(i for i, row in enumerate(journal["attempts"]) if row["attempt_id"] == success["attempt_id"])
                next_step = links[index + 1]["step_id"] if ordered_links and index + 1 < len(links) else float("inf")
                # Calls in one ACTF step may run concurrently; their list order is not execution order.
                inspected = ordered_links and link is not None and any(
                    row["sha256"] == success["image_sha256"] and link["step_id"] < row["step_id"] < next_step for row in viewed)
                process_audit["render_views"].append({"attempt_id": success["attempt_id"], "order_verified": bool(inspected)})
            process_audit["render_view_order_verified"] = bool(successes) and all(row["order_verified"] for row in process_audit["render_views"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        check("artifact", "readable_evidence", False, str(exc))
    for gate in gates.values():
        gate["passed"] = bool(gate["checks"]) and all(row["passed"] for row in gate["checks"])
    inventory = evidence_inventory(sample)
    implementation = implementation_identity()
    frozen = {"version": VERSION, "contract": contract, "evidence": inventory, "implementation": implementation}
    fingerprint = hashlib.sha256(json.dumps(frozen, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    three_gates = all(gate["passed"] for gate in gates.values())
    return {"schema_version": VERSION, "fingerprint": fingerprint, "contract": contract,
            "audited_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "implementation": implementation,
            "evidence": inventory, "gates": gates, "three_gates_passed": three_gates,
            "qualified": three_gates and expected_profile["qualification_ready"] and not report_only,
            "failure_class": ("evidence_check_failed" if not three_gates else "policy_pending"
                              if not expected_profile["qualification_ready"] else "diagnostic_only" if report_only else None),
            "profile_verdict": "evaluated",
            "qualification_scope": "artifact_and_trajectory_evidence",
            "prompt_compliance": "not_fully_audited", "process_audit": process_audit,
            "visual_quality": "not_evaluated", "report_only": report_only,
            "render_evidence": successes, "viewed_image_evidence": viewed,
            "code_revision_count": sum(a["code_sha256"] != b["code_sha256"] for a, b in zip(successes, successes[1:]))}


def write_report(run_dir: Path, sample_relative: str, report: dict) -> Path:
    run_dir = safe_root(run_dir)
    _safe(run_dir, sample_relative)
    identity = hashlib.sha256(sample_relative.encode()).hexdigest()
    path = run_dir / "chart2code" / "acceptance" / (identity + ".json")
    _safe(run_dir, path.relative_to(run_dir).as_posix())
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {**report, "sample_path": sample_relative})
    return path
