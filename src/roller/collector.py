from __future__ import annotations

import json
import logging
import queue
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from roller.harnesses.base import Harness
from roller.models import HarnessResult, RunRequest
from roller.storage import atomic_write_json, safe_component, write_jsonl

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
        result_path = sample_dir / "result.json"
        if not self.config.resume or not result_path.is_file():
            return None
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("status") != "ok":
            return None
        trace_value = payload.get("trace")
        trace_path = (
            Path(trace_value)
            if isinstance(trace_value, str)
            else Path("rollout/trace.jsonl")
        )
        if not trace_path.is_absolute():
            trace_path = sample_dir / trace_path
        if not trace_path.is_file():
            return None
        provenance_path = sample_dir / "provenance.json"
        if request.bundle is not None:
            if not request.bundle.resume_safe:
                logger.info(
                    "[%s | %s] context capture=none disables resume",
                    request.task.id,
                    request.model,
                )
                return None
            try:
                provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            if provenance.get("task_bundle_digest") != request.bundle.manifest_digest:
                logger.info(
                    "[%s | %s] task bundle changed; rerunning",
                    request.task.id,
                    request.model,
                )
                return None
        payload["status"] = "skipped"
        payload["resumed_from_status"] = "ok"
        return payload

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

        sample_dir.mkdir(parents=True, exist_ok=True)
        request_payload = {
            "request_index": request.index,
            "task_id": request.task.id,
            "source_index": request.task.source_index,
            "model": request.model,
            "timeout_seconds": request.timeout_seconds,
            "prompts": list(request.task.prompts),
            "env_names": list(request.task.env_names),
            "thinking": request.task.thinking,
            "task_bundle": (
                {
                    "manifest": request.bundle.manifest_path.relative_to(
                        self.run_dir
                    ).as_posix(),
                    "manifest_digest": request.bundle.manifest_digest,
                    "input_tree_digest": request.bundle.input_tree_digest,
                    "input_manifest_digest": request.bundle.input_manifest_digest,
                    "context_manifest_digest": request.bundle.context_manifest_digest,
                    "resume_safe": request.bundle.resume_safe,
                }
                if request.bundle is not None
                else None
            ),
        }
        atomic_write_json(sample_dir / "request.json", request_payload)

        started_at = _now()
        attempt_rows: list[dict[str, Any]] = []
        final = HarnessResult.error_result("harness did not run")
        for attempt in range(1, self.config.retries + 2):
            harness = self.harness_factory(worker_slot)
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

        result = {
            "request_index": request.index,
            "task_id": request.task.id,
            "source_index": request.task.source_index,
            "model": request.model,
            "harness": getattr(harness, "name", "unknown"),
            "status": final.status,
            "error": final.error,
            "elapsed_seconds": round(final.elapsed_seconds, 3),
            "usage": final.usage,
            "trace": final.trace_path,
            "metadata": final.metadata,
            "attempts": attempt_rows,
            "output_dir": str(sample_dir.resolve()),
            "started_at": started_at,
            "finished_at": _now(),
        }
        atomic_write_json(sample_dir / "result.json", result)
        return result

    def collect(self, requests: list[RunRequest]) -> list[dict[str, Any]]:
        run_dir = self.run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        slots: queue.Queue[int] = queue.Queue()
        for slot in range(self.config.parallel):
            slots.put(slot)

        def with_slot(request: RunRequest) -> dict[str, Any]:
            slot = slots.get()
            try:
                return self._run_one(request, slot)
            finally:
                slots.put(slot)

        results: list[dict[str, Any]] = []
        if self.config.parallel == 1:
            results = [with_slot(request) for request in requests]
        else:
            with ThreadPoolExecutor(max_workers=self.config.parallel, thread_name_prefix="roller") as pool:
                futures = {pool.submit(with_slot, request): request for request in requests}
                for future in as_completed(futures):
                    request = futures[future]
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        logger.exception("[%s | %s] collector exception", request.task.id, request.model)
                        results.append(
                            {
                                "request_index": request.index,
                                "task_id": request.task.id,
                                "model": request.model,
                                "harness": "unknown",
                                "status": "error",
                                "error": str(exc),
                                "output_dir": str(self._sample_dir(request).resolve()),
                            }
                        )

        results.sort(key=lambda row: int(row.get("request_index", -1)))
        write_jsonl(run_dir / "manifest.jsonl", results)
        totals = {
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
        summary = {
            "total": len(results),
            "ok": sum(result.get("status") == "ok" for result in results),
            "error": sum(result.get("status") == "error" for result in results),
            "timeout": sum(result.get("status") == "timeout" for result in results),
            "skipped": sum(result.get("status") == "skipped" for result in results),
            "usage": totals,
            "run_dir": str(run_dir),
            "finished_at": _now(),
        }
        summary["completed"] = summary["ok"] + summary["skipped"]
        summary["failed"] = summary["error"] + summary["timeout"]
        atomic_write_json(run_dir / "summary.json", summary)
        return results
