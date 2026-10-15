from __future__ import annotations

import json
import inspect
import logging
import queue
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from roller.harnesses.base import Harness
from roller.capture import read_jsonl, recover_capture_retention, validate_capture
from roller.integrity import (
    has_legacy_claude_sidechain,
    trajectory_conversion_unavailable,
    validate_resumable_sample,
)
from roller.models import HarnessResult, RunRequest
from roller.sft import SFTConversionError, validate_sft_record, write_sft
from roller.storage import atomic_write_json, safe_component, write_jsonl
from roller.trajectory_audit import (
    record_training_audits, sft_exclusion_reason, training_summary,
)

logger = logging.getLogger(__name__)
# New factories may inspect the request to select a task-level harness.  The
# one-argument form remains supported for downstream callers and older tests.
HarnessFactory = Callable[..., Harness]
RUN_CHECKPOINT_RESULT_INTERVAL = 25
RUN_CHECKPOINT_TIME_INTERVAL_SECONDS = 30.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sample_directory(run_dir: Path, request: RunRequest) -> Path:
    model_dir = safe_component(request.display_model)
    task_dir = f"{request.task.source_index:08d}_{safe_component(request.task.id)}"
    if request.sample_count > 1:
        task_dir += f"__sample_{request.sample_index + 1:03d}"
    return run_dir / "samples" / model_dir / task_dir


def check_retired_cot_samples(run_dir: Path, requests: list[RunRequest]) -> None:
    """Recover capture publications and reject incompatible old samples before collection."""

    for request in requests:
        sample_dir = _sample_directory(run_dir, request)
        for base in (sample_dir, sample_dir / "user_simulator"):
            info_path = base / "info.json"
            capture_root = base / "capture"
            if capture_root.is_dir():
                interrupted = {
                    match.group(1)
                    for entry in capture_root.iterdir()
                    if (match := re.fullmatch(
                        r"\.(attempt-\d+)\.retention-(?:stage|backup)", entry.name
                    )) is not None
                }
                for attempt in interrupted:
                    recover_capture_retention(
                        capture_root / attempt, prefer_raw=not info_path.is_file()
                    )
                if interrupted and not info_path.is_file():
                    raise ValueError(
                        f"{base}: capture retention was interrupted; a valid capture attempt was recovered, "
                        "but the sample has no finalized info.json. Resume stopped before rerunning the model."
                    )
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(info, dict) and info.get("harness") == "claude-code" and info.get("status") == "ok":
                actf_path = base / "trace_ACTF.json"
                try:
                    actf = json.loads(actf_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    actf = None
                if isinstance(actf, dict) and has_legacy_claude_sidechain(actf):
                    raise ValueError(
                        f"{actf_path}: legacy Claude Code sidechain steps are mixed into root ACTF/SFT; "
                        "resume stopped before changing the run or calling the model. "
                        "Reproject the existing raw trace offline or use a new run directory."
                    )
            runtime = info.get("runtime") if isinstance(info, dict) else None
            if not isinstance(runtime, dict):
                continue
            for key in ("external_thinking", "signature_recovery"):
                value = runtime.get(key)
                mode = value.get("mode") if isinstance(value, dict) else value
                if mode in ("external", "external_always", "fill", "prefer"):
                    raise ValueError(
                        f"{info_path}: {key}={mode} is retired; use a new run "
                        "directory. Existing trajectory files were not changed."
                    )


@dataclass(frozen=True)
class CollectionConfig:
    run_dir: Path
    parallel: int = 1
    retries: int = 0
    resume: bool = False
    keep_aux_traces: bool = False
    capture_cache_trace: bool = True
    keep_thinking: bool = True

    def __post_init__(self) -> None:
        if self.parallel < 1:
            raise ValueError("parallel must be at least 1")
        if self.retries < 0:
            raise ValueError("retries cannot be negative")


class Collector:
    def __init__(self, config: CollectionConfig, harness_factory: HarnessFactory) -> None:
        self.config = config
        self.harness_factory = harness_factory
        self.run_dir = config.run_dir.expanduser().resolve()
        try:
            parameters = list(inspect.signature(harness_factory).parameters.values())
            positional = [
                parameter
                for parameter in parameters
                if parameter.kind
                in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
            ]
            self._factory_accepts_request = any(
                parameter.kind == inspect.Parameter.VAR_POSITIONAL
                for parameter in parameters
            ) or len(positional) >= 2
            self._factory_request_keyword = (
                not self._factory_accepts_request
                and any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters)
            )
        except (TypeError, ValueError):
            self._factory_accepts_request = True
            self._factory_request_keyword = False

    def _make_harness(self, worker_slot: int, request: RunRequest) -> Harness:
        if self._factory_accepts_request:
            return self.harness_factory(worker_slot, request)
        if self._factory_request_keyword:
            return self.harness_factory(worker_slot, request=request)
        return self.harness_factory(worker_slot)

    def _sample_dir(self, request: RunRequest) -> Path:
        return _sample_directory(self.run_dir, request)

    def _load_resumable(
        self, sample_dir: Path, request: RunRequest
    ) -> dict[str, Any] | None:
        info_path = sample_dir / "info.json"
        if not self.config.resume or not info_path.is_file():
            return None
        try:
            payload = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if request.bundle is not None:
            if not request.bundle.resume_safe:
                logger.info(
                    "[%s | %s] context capture=none disables resume",
                    request.task.id,
                    request.model,
                )
                return None
            provenance = payload.get("provenance")
            if not isinstance(provenance, dict) or provenance.get(
                "task_bundle_digest"
            ) != request.bundle.manifest_digest:
                logger.info(
                    "[%s | %s] task bundle changed; rerunning",
                    request.task.id,
                    request.model,
                )
                return None
        try:
            validate_resumable_sample(
                sample_dir,
                request,
                payload,
                require_wire_trace=self.config.keep_aux_traces,
                require_cache_trace=(
                    self.config.keep_aux_traces and self.config.capture_cache_trace
                ),
            )
        except ValueError as exc:
            logger.info(
                "[%s | %s] existing sample failed integrity validation; rerunning: %s",
                request.task.id,
                request.model,
                exc,
            )
            return None
        resumed = dict(payload)
        resumed["status"] = "skipped"
        resumed["resumed_from_status"] = "ok"
        return resumed

    @staticmethod
    def _trace_source(sample_dir: Path, value: str | None) -> Path | None:
        if not value:
            return None
        path = Path(value)
        if not path.is_absolute():
            path = sample_dir / path
        return path if path.is_file() else None

    @staticmethod
    def _prune_sample(sample_dir: Path, keep_aux_traces: bool = False) -> None:
        keep = {
            "trace.jsonl",
            "trace_ACTF.json",
            "trace_SFT.json",
            "tools_capture.jsonl",  # Exact Codex Responses tool snapshots.
            "signature_recovery.jsonl",  # Preserve historical artifacts; never generated now.
            "info.json",
            "results",
            "conversation.json",
            "user_simulator",
            "capture",  # Formal per-attempt calls/native evidence, not diagnostics.
            # Written by bench grading, which runs before the container dies.
            "score.json",
        }
        if keep_aux_traces:
            # Full mode: retain the diagnostic layers alongside the ACTF trajectory.
            keep |= {"cache_trace.jsonl", "wire_trace.jsonl"}
        for path in sample_dir.iterdir():
            if path.name in keep:
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)

    def _finalize_sample(
        self,
        request: RunRequest,
        sample_dir: Path,
        harness_name: str,
        final: HarnessResult,
        attempts: list[dict[str, Any]],
        started_at: str,
    ) -> dict[str, Any]:
        metadata = final.metadata if isinstance(final.metadata, dict) else {}
        trace_source = self._trace_source(sample_dir, final.trace_path)
        trajectory_source = self._trace_source(sample_dir, final.trajectory_path)
        trace_path = sample_dir / "trace.jsonl"
        actf_path = sample_dir / "trace_ACTF.json"
        sft_path = sample_dir / "trace_SFT.json"
        if trace_source is None and final.status == "ok":
            final.status = "error"
            final.error = "harness finished but no raw trace was found"
        elif (
            trajectory_source is None
            and final.status == "ok"
            and not (
                harness_name in {"opencode", "openclaw"}
                and trajectory_conversion_unavailable(metadata) == "actf"
            )
        ):
            final.status = "error"
            final.error = "harness finished but no canonical ACTF trajectory was found"

        if trace_source is not None:
            if trace_source.resolve() != trace_path.resolve():
                shutil.copy2(trace_source, trace_path)

        trajectory = None
        if trajectory_source is not None:
            if trajectory_source.resolve() != actf_path.resolve():
                shutil.copy2(trajectory_source, actf_path)
            try:
                trajectory = json.loads(actf_path.read_text(encoding="utf-8"))
                if not isinstance(trajectory, dict):
                    raise ValueError("canonical ACTF trajectory must be a JSON object")
                if final.status == "ok":
                    write_sft(sft_path, trajectory)
            except SFTConversionError as exc:
                logger.warning(
                    "[%s | %s] SFT export unavailable: %s",
                    request.task.id,
                    request.model,
                    exc,
                )
                metadata["trajectory_conversion"] = {
                    "status": "unavailable",
                    "target": "sft",
                    "error": str(exc),
                }
                sft_path.unlink(missing_ok=True)
            except Exception as exc:
                logger.exception(
                    "[%s | %s] trajectory conversion failed",
                    request.task.id,
                    request.model,
                )
                final.status = "error"
                final.error = f"trajectory conversion failed: {exc}"
        # Failed/partial rollouts are diagnostic evidence, not training exports.
        # Also remove stale exports left by an earlier attempt or failed write.
        if final.status != "ok":
            sft_path.unlink(missing_ok=True)
        record_training_audits(
            metadata, trajectory if isinstance(trajectory, dict) else None,
            sample_dir / "wire_trace.jsonl",
        )
        if attempts and attempts[-1].get("status") != final.status:
            attempts[-1]["status"] = final.status
            attempts[-1]["error"] = final.error

        provenance = metadata.get("provenance")
        if not isinstance(provenance, dict):
            provenance = {}
        if request.bundle is not None:
            provenance = {
                **provenance,
                "task_bundle_digest": request.bundle.manifest_digest,
                "input_tree_digest": request.bundle.input_tree_digest,
                "input_manifest_digest": request.bundle.input_manifest_digest,
                "context_manifest_digest": request.bundle.context_manifest_digest,
                "resume_safe": request.bundle.resume_safe,
            }

        result = {
            "schema_version": "roller.sample/v1",
            "request_index": request.index,
            "sample_index": request.sample_index,
            "sample_count": request.sample_count,
            "task_id": request.task.id,
            "source_index": request.task.source_index,
            "model": request.model,
            "model_label": request.display_model,
            "harness": harness_name,
            "status": final.status,
            "error": final.error,
            "usage": final.usage,
            "attempts": attempts,
            "timing": {
                "elapsed_seconds": round(final.elapsed_seconds, 3),
                "started_at": started_at,
                "finished_at": _now(),
            },
            "trajectory": {
                "raw": "trace.jsonl" if trace_path.is_file() else None,
                "raw_schema": metadata.get("raw_trace_schema"),
                "actf": "trace_ACTF.json" if actf_path.is_file() else None,
                "actf_schema": "ACTF_v1.0" if actf_path.is_file() else None,
                "sft": "trace_SFT.json" if sft_path.is_file() else None,
            },
            "conversation": metadata.get("conversation"),
            "output_collection": metadata.get("output_collection"),
            "runtime": {
                key: value
                for key, value in metadata.items()
                if key not in {"conversation", "output_collection", "provenance"}
            },
            "provenance": provenance,
        }
        self._prune_sample(sample_dir, keep_aux_traces=self.config.keep_aux_traces)
        atomic_write_json(sample_dir / "info.json", result)
        return result

    def _run_one(self, request: RunRequest, worker_slot: int) -> dict[str, Any]:
        sample_dir = self._sample_dir(request)
        resumed = self._load_resumable(sample_dir, request)
        if resumed is not None:
            resumed.update(
                {
                    "request_index": request.index,
                    "sample_index": request.sample_index,
                    "sample_count": request.sample_count,
                    "task_id": request.task.id,
                    "source_index": request.task.source_index,
                    "model": request.model,
                    "model_label": request.display_model,
                }
            )
            logger.info("[%s | %s] already complete; skipping", request.task.id, request.model)
            return resumed

        if sample_dir.exists():
            shutil.rmtree(sample_dir)
        sample_dir.mkdir(parents=True, exist_ok=True)
        started_at = _now()
        attempt_rows: list[dict[str, Any]] = []
        final = HarnessResult.error_result("harness did not run")
        harness_name = "unknown"
        for attempt in range(1, self.config.retries + 2):
            harness = self._make_harness(worker_slot, request)
            harness_name = getattr(harness, "name", "unknown")
            attempt_start = time.perf_counter()
            try:
                final = harness.run(request, sample_dir, attempt)
            except Exception as exc:
                logger.exception("[%s | %s] harness exception", request.task.id, request.model)
                final = HarnessResult.error_result(str(exc), time.perf_counter() - attempt_start)
            attempt_rows.append(
                {
                    "attempt": attempt,
                    "status": final.status,
                    "error": final.error,
                    "elapsed_seconds": round(final.elapsed_seconds, 3),
                }
            )
            if final.status == "ok":
                break
            if attempt <= self.config.retries:
                logger.warning(
                    "[%s | %s] attempt %d failed; retrying",
                    request.task.id,
                    request.model,
                    attempt,
                )

        return self._finalize_sample(
            request, sample_dir, harness_name, final, attempt_rows, started_at
        )

    @staticmethod
    def _usage_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
        totals: dict[str, Any] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "request_count": 0,
        }
        for result in results:
            usage = result.get("usage")
            if not isinstance(usage, dict):
                continue
            for key in totals:
                value = usage.get(key, 0)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[key] += value
        totals["cost_usd"] = round(float(totals["cost_usd"]), 6)
        return totals

    def _sample_dir_from_result(self, result: dict[str, Any]) -> Path:
        model = safe_component(
            str(result.get("model_label", result.get("model", "unknown")))
        )
        source_index = int(result.get("source_index", result.get("request_index", 0)))
        task = safe_component(str(result.get("task_id", "unknown")))
        task_dir = f"{source_index:08d}_{task}"
        sample_count = int(result.get("sample_count", 1))
        if sample_count > 1:
            sample_index = int(result.get("sample_index", 0))
            task_dir += f"__sample_{sample_index + 1:03d}"
        return self.run_dir / "samples" / model / task_dir

    def _write_run_info(
        self,
        results: list[dict[str, Any]],
        *,
        total_requests: int,
        state: str,
    ) -> None:
        info_path = self.run_dir / "info.json"
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            info = {"schema_version": "roller.run/v1"}
        summary = {
            "total": total_requests,
            "processed": len(results),
            "pending": max(0, total_requests - len(results)),
            "ok": sum(result.get("status") == "ok" for result in results),
            "error": sum(result.get("status") == "error" for result in results),
            "timeout": sum(result.get("status") == "timeout" for result in results),
            "skipped": sum(result.get("status") == "skipped" for result in results),
            "usage": self._usage_summary(results),
            "training": training_summary(results),
        }
        summary["completed"] = summary["ok"] + summary["skipped"]
        summary["failed"] = summary["error"] + summary["timeout"]
        info.update(
            {
                "state": state,
                "summary": summary,
                "samples": [
                    {
                        "request_index": result.get("request_index"),
                        "sample_index": result.get("sample_index", 0),
                        "sample_count": result.get("sample_count", 1),
                        "task_id": result.get("task_id"),
                        "model": result.get("model"),
                        "model_label": result.get("model_label", result.get("model")),
                        "status": result.get("status"),
                        "error": result.get("error"),
                        "path": self._sample_dir_from_result(result)
                        .relative_to(self.run_dir)
                        .as_posix(),
                        "usage": result.get("usage", {}),
                    }
                    for result in results
                ],
                "updated_at": _now(),
            }
        )
        if state == "finished":
            info["finished_at"] = info["updated_at"]
        else:
            info.pop("finished_at", None)
        atomic_write_json(info_path, info)

    def _write_sft_dataset(self, results: list[dict[str, Any]]) -> None:
        def rows() -> Iterator[dict[str, Any]]:
            for result in results:
                if sft_exclusion_reason(result) is not None:
                    continue
                path = self._sample_dir_from_result(result) / "trace_SFT.json"
                try:
                    row = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise RuntimeError(
                        f"cannot aggregate SFT sample {path}: {exc}"
                    ) from exc
                if not isinstance(row, dict):
                    raise RuntimeError(f"invalid SFT sample shape: {path}")
                try:
                    validate_sft_record(row)
                except ValueError as exc:
                    raise RuntimeError(
                        f"invalid SFT sample shape ({exc}): {path}"
                    ) from exc
                yield row

        write_jsonl(self.run_dir / "sft.jsonl", rows())
        def calls() -> Iterator[dict[str, Any]]:
            for result in results:
                if result.get("status") not in {"ok", "skipped"}:
                    continue
                capture = result.get("runtime", {}).get("capture")
                if not isinstance(capture, dict) or not capture.get("manifest"):
                    continue
                sample = self._sample_dir_from_result(result)
                validate_capture(sample, capture)
                manifest_path = sample / capture["manifest"]
                for call in read_jsonl(manifest_path.parent / "calls.jsonl"):
                    if call.get("export_status") == "available":
                        yield {**call, "sample": sample.relative_to(self.run_dir).as_posix(),
                               "capture_manifest": capture["manifest"],
                               "task_id": result["task_id"], "model_label": result.get("model_label"),
                               "sample_index": result.get("sample_index", 0)}
        # Separate schema; historical assistant content is input, output is the
        # current response. Never append these records to legacy sft.jsonl.
        if any(isinstance(r.get("runtime", {}).get("capture"), dict) for r in results):
            write_jsonl(self.run_dir / "calls.jsonl", calls())

    def collect(self, requests: list[RunRequest]) -> list[dict[str, Any]]:
        check_retired_cot_samples(self.run_dir, requests)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        total_requests = len(requests)
        self._write_run_info(
            [], total_requests=total_requests, state="running"
        )
        slots: queue.Queue[int] = queue.Queue()
        for slot in range(self.config.parallel):
            slots.put(slot)

        def with_slot(request: RunRequest) -> dict[str, Any]:
            slot = slots.get()
            try:
                return self._run_one(request, slot)
            finally:
                slots.put(slot)

        def collector_error(request: RunRequest, exc: Exception) -> dict[str, Any]:
            logger.exception(
                "[%s | %s] collector exception", request.task.id, request.model
            )
            sample_dir = self._sample_dir(request)
            sample_dir.mkdir(parents=True, exist_ok=True)
            self._prune_sample(sample_dir, keep_aux_traces=self.config.keep_aux_traces)
            result = {
                "schema_version": "roller.sample/v1",
                "request_index": request.index,
                "sample_index": request.sample_index,
                "sample_count": request.sample_count,
                "source_index": request.task.source_index,
                "task_id": request.task.id,
                "model": request.model,
                "model_label": request.display_model,
                "harness": "unknown",
                "status": "error",
                "error": str(exc),
                "usage": {},
                "attempts": [],
            }
            atomic_write_json(sample_dir / "info.json", result)
            return result

        results: list[dict[str, Any]] = []
        checkpoint_count = 0
        checkpoint_time = time.monotonic()

        def checkpoint(state: str, *, force: bool = False) -> list[dict[str, Any]]:
            nonlocal checkpoint_count, checkpoint_time
            now = time.monotonic()
            if (
                state == "running"
                and not force
                and len(results) != 1
                and len(results) - checkpoint_count < RUN_CHECKPOINT_RESULT_INTERVAL
                and now - checkpoint_time < RUN_CHECKPOINT_TIME_INTERVAL_SECONDS
            ):
                return []
            ordered = sorted(
                results, key=lambda row: int(row.get("request_index", -1))
            )
            self._write_run_info(
                ordered,
                total_requests=total_requests,
                state=state,
            )
            checkpoint_count = len(results)
            checkpoint_time = now
            return ordered

        try:
            if self.config.parallel == 1:
                for request in requests:
                    try:
                        results.append(with_slot(request))
                    except Exception as exc:
                        results.append(collector_error(request, exc))
                    checkpoint("running")
            else:
                with ThreadPoolExecutor(
                    max_workers=self.config.parallel, thread_name_prefix="roller"
                ) as pool:
                    futures = {
                        pool.submit(with_slot, request): request for request in requests
                    }
                    for future in as_completed(futures):
                        request = futures[future]
                        try:
                            results.append(future.result())
                        except Exception as exc:
                            results.append(collector_error(request, exc))
                        checkpoint("running")
        except BaseException:
            ordered = checkpoint("interrupted", force=True)
            try:
                self._write_sft_dataset(ordered)
            except Exception:
                logger.exception("failed to aggregate partial SFT dataset")
            raise

        ordered = checkpoint("finalizing", force=True)
        try:
            self._write_sft_dataset(ordered)
        except BaseException:
            checkpoint("finalization_error", force=True)
            raise
        checkpoint("finished", force=True)
        return ordered
