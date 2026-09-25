"""Side-only lifecycle guard. Main Collector remains task-agnostic.

All existing samples are checked before Collector can write run metadata or
launch a harness. No automatic repair, deletion, or paid retry of old evidence.
The production launcher must pass resolved harness configuration in execution;
this module deliberately does not invent model/endpoint defaults.
"""
from __future__ import annotations

from copy import deepcopy
import fcntl
import os
from pathlib import Path

from roller import __version__
from roller.collector import Collector
from roller.integrity import validate_resumable_sample
from roller.native_images import attachment_source
from roller.run_lock import build_run_lock
from roller.storage import atomic_write_json
from roller_chart2code.acceptance import audit_sample, file_hash, json_object, object_field, safe_root, write_report
from roller_chart2code.profiles import freeze_profile
from roller_chart2code.render_journal import _safe


class ResumeRejected(ValueError):
    """A preflight failure; no harness has been invoked."""


class EvidenceChanged(BaseException):
    """Abort without entering Collector's destructive per-sample error handler.

    Intentionally NOT Exception: Collector catches those and prunes/rewrites
    the sample. It already preserves evidence on BaseException interruption.
    """


def contract_for(request, profile_name: str, reference: str, *, max_renders=None, require_wire_trace=False) -> dict:
    profile = freeze_profile(profile_name, reference, max_renders=max_renders)
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != reference:
        raise ValueError("reference must be a canonical workspace-relative path")
    if tuple(text.strip() for text in request.task.prompts) != (profile["prompt"].strip(),):
        raise ValueError("request prompt differs from the frozen single-turn profile")
    reference_file = attachment_source(request, reference)
    return {"profile": profile, "reference_sha256": file_hash(reference_file),
            "capture": {"require_wire_trace": require_wire_trace}, "identity": {
        "task_id": request.task.id, "model": request.model, "model_label": request.display_model,
        "harness": request.task.harness, "request_index": request.index,
        "sample_index": request.sample_index, "sample_count": request.sample_count,
        "source_index": request.task.source_index}}


class Chart2CodeCollector(Collector):
    def __init__(self, config, harness_factory, *, contracts, execution):
        safe_root(config.run_dir)
        super().__init__(config, harness_factory)
        self.contracts = deepcopy(contracts)
        self.execution = deepcopy(execution)
        self._approved = {}
        self.task_summary = None

    def _safe_sample(self, request):
        path = self._sample_dir(request)
        return _safe(self.run_dir, path.relative_to(self.run_dir).as_posix())

    def _audit(self, request):
        path = self._safe_sample(request)
        info = json_object(_safe(path, "info.json"))
        if request.bundle is None or not request.bundle.resume_safe:
            raise ResumeRejected("resume requires an immutable, resume-safe task bundle")
        if object_field(info.get("provenance", {}), "provenance").get("task_bundle_digest") != request.bundle.manifest_digest:
            raise ResumeRejected("sample task bundle changed; preserve it and choose a new run directory")
        validate_resumable_sample(path, request, info,
            require_wire_trace=self.config.keep_aux_traces,
            require_cache_trace=self.config.keep_aux_traces and self.config.capture_cache_trace)
        report = audit_sample(path, self.contracts[request.index])
        return info, report

    def _preflight(self, requests):
        indexes, paths, entries = set(), set(), []
        existing = []
        for request in requests:
            path = self._safe_sample(request)
            if request.index in indexes or path in paths:
                raise ValueError("duplicate request index or sample path")
            indexes.add(request.index)
            paths.add(path)
            contract = self.contracts[request.index]
            profile = contract["profile"]
            expected = contract_for(request, profile["name"], profile["reference"],
                max_renders=profile["max_render_attempts"], require_wire_trace=self.config.keep_aux_traces)
            if contract != expected:
                raise ValueError("task contract differs from the current request/input")
            entries.append({"sample_path": path.relative_to(self.run_dir).as_posix(), "contract": contract})
            if path.exists():
                existing.append(request)
        if indexes != self.contracts.keys():
            raise ValueError("contracts must cover exactly the requested samples")
        if existing and not self.config.resume:
            raise ResumeRejected("existing sample directories require explicit resume; none were changed")
        lock = build_run_lock(requests, roller_version=__version__,
            execution={**self.execution, "chart2code_collection": {
                "retries": self.config.retries,
                "retry_backoff_seconds": self.config.retry_backoff_seconds,
                "keep_aux_traces": self.config.keep_aux_traces,
                "capture_cache_trace": self.config.capture_cache_trace,
                "keep_thinking": self.config.keep_thinking}}, model_config=None)
        lock_path = _safe(self.run_dir, "lock.json")
        if lock_path.exists():
            if json_object(lock_path) != lock:
                raise ResumeRejected("run configuration differs from lock.json; use a new run directory")
        elif existing:
            raise ResumeRejected("existing samples have no trusted run lock; no automatic adoption or rerun")
        manifest = {"schema_version": "roller.chart2code-contracts/v1", "entries": entries}
        manifest_path = _safe(self.run_dir, "chart2code/contracts.json")
        if manifest_path.exists() and json_object(manifest_path) != manifest:
            raise ResumeRejected("frozen Chart2Code contracts changed; use a new run directory")
        self._approved = {}
        rejected = []
        for request in existing:
            try:
                info, report = self._audit(request)
                write_report(self.run_dir, self._safe_sample(request).relative_to(self.run_dir).as_posix(), report)
                if not report["qualified"]:
                    raise ResumeRejected("task acceptance failed")
                self._approved[request.index] = (info, report["fingerprint"])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                rejected.append(f"request {request.index}: {exc}")
        if rejected:
            raise ResumeRejected("Resume refused before any harness call; original samples preserved: " + "; ".join(rejected))
        # Only now may the run/contract metadata be initialized.
        if not lock_path.exists():
            atomic_write_json(lock_path, lock)
        if not manifest_path.exists():
            atomic_write_json(manifest_path, manifest)

    def _load_resumable(self, sample_dir, request):
        # Called immediately before the inherited _run_one could remove a path.
        # A concurrently appeared/changed sample must escape Collector's prune handler.
        try:
            self._safe_sample(request)
        except ValueError as exc:
            raise EvidenceChanged("sample became an unsafe symlink after preflight") from exc
        if not sample_dir.exists():
            if request.index in self._approved:
                raise EvidenceChanged("preflight-approved sample disappeared")
            return None
        if request.index not in self._approved:
            raise EvidenceChanged("sample appeared after preflight; refusing to overwrite")
        try:
            info, report = self._audit(request)
            if not report["qualified"] or report["fingerprint"] != self._approved[request.index][1]:
                raise EvidenceChanged("sample evidence changed after preflight")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise EvidenceChanged("sample became unreadable after preflight") from exc
        return {**info, "status": "skipped", "resumed_from_status": "ok"}

    def collect(self, requests):
        # Cooperative process lock covers preflight, collection and final audit.
        lock_path = _safe(self.run_dir, "chart2code/lifecycle.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "w") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ResumeRejected("another Chart2Code lifecycle owns this run") from exc
            self._preflight(requests)
            results = super().collect(requests)
            # Never rewrite execution status to mean task qualification.
            from roller_chart2code.export import audit_run
            self.task_summary = audit_run(self.run_dir)
            return results
