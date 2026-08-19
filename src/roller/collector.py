from __future__ import annotations

import json
import logging
import queue
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from roller.actf import convert_openclaw_trace
from roller.harnesses.base import Harness
from roller.models import HarnessResult, RunRequest
from roller.storage import atomic_write_json, safe_component

logger = logging.getLogger(__name__)
HarnessFactory = Callable[[int], Harness]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class CollectionConfig:
    run_dir: Path
    parallel: int = 1
    retries: int = 0
    resume: bool = False

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

    def _sample_dir(self, request: RunRequest) -> Path:
        model_dir = safe_component(request.model)
        task_dir = f"{request.task.source_index:08d}_{safe_component(request.task.id)}"
        return self.run_dir / "samples" / model_dir / task_dir

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
        if payload.get("status") != "ok":
            return None
        if not (sample_dir / "trace.jsonl").is_file() or not (
            sample_dir / "trace_ACTF.json"
        ).is_file():
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
    def _prune_sample(sample_dir: Path) -> None:
        keep = {"trace.jsonl", "trace_ACTF.json", "info.json", "results"}
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
        trace_source = self._trace_source(sample_dir, final.trace_path)
        trace_path = sample_dir / "trace.jsonl"
        actf_path = sample_dir / "trace_ACTF.json"
        if trace_source is None and final.status == "ok":
            final.status = "error"
            final.error = "harness finished but no raw trace was found"
        elif trace_source is not None:
            if trace_source.resolve() != trace_path.resolve():
                shutil.copy2(trace_source, trace_path)
            try:
                convert_openclaw_trace(
                    trace_path, actf_path, user_messages=request.task.prompts
                )
            except Exception as exc:
                logger.exception("[%s | %s] ACTF conversion failed", request.task.id, request.model)
                final.status = "error"
                final.error = f"ACTF conversion failed: {exc}"
        if attempts and attempts[-1].get("status") != final.status:
            attempts[-1]["status"] = final.status
            attempts[-1]["error"] = final.error

        metadata = final.metadata if isinstance(final.metadata, dict) else {}
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
            "task_id": request.task.id,
            "source_index": request.task.source_index,
            "model": request.model,
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
                "actf": "trace_ACTF.json" if actf_path.is_file() else None,
                "actf_schema": "ACTF_v1.0" if actf_path.is_file() else None,
            },
            "output_contract": metadata.get("output_contract"),
            "runtime": {
                key: value
                for key, value in metadata.items()
                if key not in {"output_contract", "provenance"}
            },
            "provenance": provenance,
        }
        self._prune_sample(sample_dir)
        atomic_write_json(sample_dir / "info.json", result)
        return result

    def _run_one(self, request: RunRequest, worker_slot: int) -> dict[str, Any]:
        sample_dir = self._sample_dir(request)
        resumed = self._load_resumable(sample_dir, request)
        if resumed is not None:
            resumed.update(
                {
                    "request_index": request.index,
                    "task_id": request.task.id,
                    "source_index": request.task.source_index,
                    "model": request.model,
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
            harness = self.harness_factory(worker_slot)
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
        model = safe_component(str(result.get("model", "unknown")))
        source_index = int(result.get("source_index", result.get("request_index", 0)))
        task = safe_component(str(result.get("task_id", "unknown")))
        return self.run_dir / "samples" / model / f"{source_index:08d}_{task}"

    def _write_run_info(self, results: list[dict[str, Any]]) -> None:
        info_path = self.run_dir / "info.json"
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            info = {"schema_version": "roller.run/v1"}
        summary = {
            "total": len(results),
            "ok": sum(result.get("status") == "ok" for result in results),
            "error": sum(result.get("status") == "error" for result in results),
            "timeout": sum(result.get("status") == "timeout" for result in results),
            "skipped": sum(result.get("status") == "skipped" for result in results),
            "usage": self._usage_summary(results),
        }
        summary["completed"] = summary["ok"] + summary["skipped"]
        summary["failed"] = summary["error"] + summary["timeout"]
        info.update(
            {
                "summary": summary,
                "samples": [
                    {
                        "request_index": result.get("request_index"),
                        "task_id": result.get("task_id"),
                        "model": result.get("model"),
                        "status": result.get("status"),
                        "error": result.get("error"),
                        "path": self._sample_dir_from_result(result)
                        .relative_to(self.run_dir)
                        .as_posix(),
                        "usage": result.get("usage", {}),
                    }
                    for result in results
                ],
                "finished_at": _now(),
            }
        )
        atomic_write_json(info_path, info)

    def collect(self, requests: list[RunRequest]) -> list[dict[str, Any]]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
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
            self._prune_sample(sample_dir)
            result = {
                "schema_version": "roller.sample/v1",
                "request_index": request.index,
                "source_index": request.task.source_index,
                "task_id": request.task.id,
                "model": request.model,
                "harness": "unknown",
                "status": "error",
                "error": str(exc),
                "usage": {},
                "attempts": [],
            }
            atomic_write_json(sample_dir / "info.json", result)
            return result

        results: list[dict[str, Any]] = []
        if self.config.parallel == 1:
            for request in requests:
                try:
                    results.append(with_slot(request))
                except Exception as exc:
                    results.append(collector_error(request, exc))
        else:
            with ThreadPoolExecutor(
                max_workers=self.config.parallel, thread_name_prefix="roller"
            ) as pool:
                futures = {pool.submit(with_slot, request): request for request in requests}
                for future in as_completed(futures):
                    request = futures[future]
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        results.append(collector_error(request, exc))

        results.sort(key=lambda row: int(row.get("request_index", -1)))
        self._write_run_info(results)
        return results
