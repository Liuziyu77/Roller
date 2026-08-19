from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from roller.collector import CollectionConfig, Collector
from roller.harnesses.base import Harness
from roller.models import HarnessResult, RunRequest, Task, TaskBundle


class FakeHarness(Harness):
    name = "fake"

    def __init__(self, state: dict, slot: int) -> None:
        self.state = state
        self.slot = slot

    def run(self, request: RunRequest, output_dir: Path, attempt: int) -> HarnessResult:
        key = (request.task.id, request.model)
        with self.state["lock"]:
            self.state["calls"][key] = self.state["calls"].get(key, 0) + 1
            self.state["active"] += 1
            self.state["max_active"] = max(self.state["max_active"], self.state["active"])
        try:
            time.sleep(0.01)
            if self.state.get("fail_first") and attempt == 1:
                return HarnessResult.error_result("transient")
            trace = output_dir / "trace.jsonl"
            trace.write_text("{}\n", encoding="utf-8")
            if request.bundle is not None:
                (output_dir / "provenance.json").write_text(
                    json.dumps({"task_bundle_digest": request.bundle.manifest_digest}),
                    encoding="utf-8",
                )
            usage = {
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 3,
                "cost_usd": 0.01,
                "request_count": 1,
            }
            return HarnessResult(
                status="ok",
                elapsed_seconds=0.01,
                usage=usage,
                trace_path=str(trace),
                metadata={"slot": self.slot},
            )
        finally:
            with self.state["lock"]:
                self.state["active"] -= 1


def make_requests() -> list[RunRequest]:
    requests: list[RunRequest] = []
    for index in range(4):
        task = Task(id=f"task-{index}", prompts=("hello",), source_index=index)
        requests.append(
            RunRequest(index=index, task=task, model="provider/model", timeout_seconds=30)
        )
    return requests


class CollectorTests(unittest.TestCase):
    def _state(self, fail_first: bool = False) -> dict:
        return {
            "lock": threading.Lock(),
            "calls": {},
            "active": 0,
            "max_active": 0,
            "fail_first": fail_first,
        }

    def test_collects_in_parallel_and_writes_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            state = self._state()
            collector = Collector(
                CollectionConfig(run_dir=run_dir, parallel=2),
                lambda slot: FakeHarness(state, slot),
            )
            results = collector.collect(make_requests())

            self.assertEqual([result["status"] for result in results], ["ok"] * 4)
            self.assertEqual(state["max_active"], 2)
            self.assertEqual(len((run_dir / "manifest.jsonl").read_text().splitlines()), 4)
            summary = json.loads((run_dir / "summary.json").read_text())
            self.assertEqual(summary["ok"], 4)
            self.assertEqual(summary["usage"]["total_tokens"], 12)

    def test_retries_then_resumes_completed_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            state = self._state(fail_first=True)
            requests = make_requests()[:1]
            collector = Collector(
                CollectionConfig(run_dir=run_dir, retries=1),
                lambda slot: FakeHarness(state, slot),
            )
            first = collector.collect(requests)
            self.assertEqual(first[0]["status"], "ok")
            self.assertEqual(len(first[0]["attempts"]), 2)
            self.assertEqual(state["calls"][("task-0", "provider/model")], 2)

            resumed = Collector(
                CollectionConfig(run_dir=run_dir, resume=True),
                lambda slot: FakeHarness(state, slot),
            ).collect(requests)
            self.assertEqual(resumed[0]["status"], "skipped")
            self.assertEqual(state["calls"][("task-0", "provider/model")], 2)

    def test_resume_is_bound_to_bundle_digest_and_privacy_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            state = self._state()
            task = Task(id="task", prompts=("hello",), source_index=0)

            def bundle(digest: str, resume_safe: bool = True) -> TaskBundle:
                bundle_root = run_dir / "tasks" / digest
                bundle_root.mkdir(parents=True, exist_ok=True)
                manifest = bundle_root / "task.json"
                manifest.write_text("{}", encoding="utf-8")
                return TaskBundle(
                    root=bundle_root,
                    inputs_root=bundle_root,
                    context_root=bundle_root,
                    manifest_path=manifest,
                    input_manifest_path=manifest,
                    context_manifest_path=manifest,
                    manifest_digest=digest,
                    input_tree_digest="inputs",
                    input_manifest_digest="input-manifest",
                    context_manifest_digest="context",
                    resume_safe=resume_safe,
                )

            first_request = RunRequest(0, task, "provider/model", 30, bundle("digest-a"))
            Collector(
                CollectionConfig(run_dir=run_dir),
                lambda slot: FakeHarness(state, slot),
            ).collect([first_request])
            skipped = Collector(
                CollectionConfig(run_dir=run_dir, resume=True),
                lambda slot: FakeHarness(state, slot),
            ).collect([first_request])
            self.assertEqual(skipped[0]["status"], "skipped")

            changed_request = RunRequest(0, task, "provider/model", 30, bundle("digest-b"))
            changed = Collector(
                CollectionConfig(run_dir=run_dir, resume=True),
                lambda slot: FakeHarness(state, slot),
            ).collect([changed_request])
            self.assertEqual(changed[0]["status"], "ok")

            private_request = RunRequest(
                0, task, "provider/model", 30, bundle("digest-b", resume_safe=False)
            )
            private = Collector(
                CollectionConfig(run_dir=run_dir, resume=True),
                lambda slot: FakeHarness(state, slot),
            ).collect([private_request])
            self.assertEqual(private[0]["status"], "ok")
            self.assertEqual(state["calls"][("task", "provider/model")], 3)


if __name__ == "__main__":
    unittest.main()
