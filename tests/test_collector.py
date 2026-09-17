from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from roller.actf import convert_openclaw_trace
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
            if self.state.get("interrupt_task") == request.task.id:
                raise KeyboardInterrupt("interrupted for test")
            if self.state.get("fail_first") and attempt == 1:
                return HarnessResult.error_result("transient")
            if self.state.get("auxiliary_traces"):
                (output_dir / "cache_trace.jsonl").write_text("", encoding="utf-8")
                (output_dir / "wire_trace.jsonl").write_text("", encoding="utf-8")
            trace = output_dir / "trace.jsonl"
            trace.write_text(
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {
                            "type": "message",
                            "message": {
                                "role": "user",
                                "content": request.task.prompts[0],
                                "timestamp": 900,
                            },
                        },
                        {
                            "type": "message",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "done"}],
                                "usage": {"input": 1, "output": 2},
                                "stopReason": "stop",
                                "timestamp": 1000,
                            },
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            trajectory = output_dir / "trajectory.json"
            convert_openclaw_trace(
                trace,
                trajectory,
                user_messages=request.task.prompts,
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
            metadata = {"slot": self.slot}
            if self.state.get("signature_recovery"):
                (output_dir / "signature_recovery.jsonl").write_text(
                    json.dumps(
                        {
                            "schema_version": "roller.claude-signature-recovery/v1",
                            "message_id": "msg-1",
                            "status": "ok",
                            "recovered_reasoning": "working",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                metadata["signature_recovery"] = {
                    "mode": "fill",
                    "candidate_count": 1,
                    "recovered_count": 1,
                    "artifact": "signature_recovery.jsonl",
                }
            if self.state.get("record_output"):
                artifact = output_dir / "results" / "artifact.txt"
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text("artifact", encoding="utf-8")
                metadata["output_collection"] = {
                    "passed": True,
                    "files": [{"path": "artifact.txt", "size": artifact.stat().st_size}],
                }
            return HarnessResult(
                status="ok",
                elapsed_seconds=0.01,
                usage=usage,
                trace_path=str(trace),
                trajectory_path=str(trajectory),
                metadata=metadata,
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

    def test_collects_in_parallel_and_writes_compact_samples(self) -> None:
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
            info = json.loads((run_dir / "info.json").read_text())
            summary = info["summary"]
            self.assertEqual(info["state"], "finished")
            self.assertEqual(summary["total"], 4)
            self.assertEqual(summary["processed"], 4)
            self.assertEqual(summary["pending"], 0)
            self.assertEqual(summary["ok"], 4)
            self.assertEqual(summary["usage"]["total_tokens"], 12)
            sample = next((run_dir / "samples").glob("*/00000000_task-0"))
            self.assertEqual(
                {path.name for path in sample.iterdir()},
                {"trace.jsonl", "trace_ACTF.json", "trace_SFT.json", "info.json"},
            )
            actf = json.loads((sample / "trace_ACTF.json").read_text())
            self.assertEqual(actf["schema_version"], "ACTF_v1.0")
            sft = json.loads((sample / "trace_SFT.json").read_text())
            self.assertEqual(set(sft), {"messages", "tools"})
            self.assertEqual(sft["messages"][1]["content"], "hello")
            dataset = [
                json.loads(line)
                for line in (run_dir / "sft.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(dataset), 4)
            self.assertEqual(dataset[0], sft)

    def test_sft_failure_does_not_overwrite_existing_harness_error(self) -> None:
        class FailedHarness(FakeHarness):
            def run(
                self,
                request: RunRequest,
                output_dir: Path,
                attempt: int,
            ) -> HarnessResult:
                result = super().run(request, output_dir, attempt)
                result.status = "error"
                result.error = "agent turn failed"
                return result

        with tempfile.TemporaryDirectory() as temporary:
            state = self._state()
            collector = Collector(
                CollectionConfig(run_dir=Path(temporary) / "run"),
                lambda slot: FailedHarness(state, slot),
            )
            with patch(
                "roller.collector.write_sft",
                side_effect=ValueError("empty terminal assistant"),
            ):
                result = collector.collect(make_requests()[:1])[0]

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "agent turn failed")
        self.assertEqual(result["attempts"][-1]["error"], "agent turn failed")
        self.assertEqual(
            result["runtime"]["trajectory_conversion_error"],
            "empty terminal assistant",
        )

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

    def test_retries_harness_factory_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = self._state()
            factory_calls = 0

            def factory(slot: int, request: RunRequest) -> FakeHarness:
                nonlocal factory_calls
                del request
                factory_calls += 1
                if factory_calls == 1:
                    raise RuntimeError("image preparation failed")
                return FakeHarness(state, slot)

            result = Collector(
                CollectionConfig(run_dir=Path(temporary) / "run", retries=1),
                factory,
            ).collect(make_requests()[:1])[0]

        self.assertEqual(result["status"], "ok")
        self.assertEqual(factory_calls, 2)
        self.assertEqual(len(result["attempts"]), 2)
        self.assertEqual(result["attempts"][0]["error"], "image preparation failed")

    def test_factory_failure_keeps_bench_metadata_and_zero_score(self) -> None:
        task = Task(
            id="bench-task",
            prompts=("hello",),
            source_index=0,
            raw={
                "bench": {
                    "name": "fake-bench",
                    "category": "demo",
                    "metadata": {"upstream_task_id": "source-task"},
                    "grade": {"code": "def grade(workspace_path): return {}"},
                }
            },
        )
        request = RunRequest(
            index=0,
            task=task,
            model="provider/model",
            timeout_seconds=30,
        )
        factory_calls = 0

        def factory(slot: int, current: RunRequest) -> FakeHarness:
            nonlocal factory_calls
            del slot, current
            factory_calls += 1
            raise RuntimeError("image preparation failed")

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            result = Collector(
                CollectionConfig(run_dir=run_dir, retries=1),
                factory,
            ).collect([request])[0]
            score = json.loads(
                next((run_dir / "samples").glob("*/*/score.json")).read_text()
            )

        self.assertEqual(factory_calls, 2)
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(result["attempts"]), 2)
        self.assertEqual(result["runtime"]["bench"]["bench"], "fake-bench")
        self.assertEqual(
            result["runtime"]["bench"]["metadata"]["upstream_task_id"],
            "source-task",
        )
        self.assertEqual(score["scores"]["overall_score"], 0.0)

    def test_resume_reruns_sample_when_canonical_sft_is_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            state = self._state()
            requests = make_requests()[:1]
            Collector(
                CollectionConfig(run_dir=run_dir),
                lambda slot: FakeHarness(state, slot),
            ).collect(requests)
            sample = next((run_dir / "samples").glob("*/*"))
            (sample / "trace_SFT.json").write_text("{}\n", encoding="utf-8")

            result = Collector(
                CollectionConfig(run_dir=run_dir, resume=True),
                lambda slot: FakeHarness(state, slot),
            ).collect(requests)

            self.assertEqual(result[0]["status"], "ok")
            self.assertEqual(state["calls"][("task-0", "provider/model")], 2)
            repaired = json.loads((sample / "trace_SFT.json").read_text())
            self.assertEqual(set(repaired), {"messages", "tools"})

    def test_resume_reruns_sample_when_a_recorded_output_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            state = self._state()
            state["record_output"] = True
            requests = make_requests()[:1]
            Collector(
                CollectionConfig(run_dir=run_dir),
                lambda slot: FakeHarness(state, slot),
            ).collect(requests)
            sample = next((run_dir / "samples").glob("*/*"))
            artifact = sample / "results" / "artifact.txt"
            artifact.unlink()

            result = Collector(
                CollectionConfig(run_dir=run_dir, resume=True),
                lambda slot: FakeHarness(state, slot),
            ).collect(requests)

            self.assertEqual(result[0]["status"], "ok")
            self.assertEqual(state["calls"][("task-0", "provider/model")], 2)
            self.assertEqual(artifact.read_text(encoding="utf-8"), "artifact")

    def test_resume_reruns_full_mode_sample_when_wire_trace_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            state = self._state()
            state["auxiliary_traces"] = True
            requests = make_requests()[:1]
            config = CollectionConfig(run_dir=run_dir, keep_aux_traces=True)
            Collector(config, lambda slot: FakeHarness(state, slot)).collect(requests)
            sample = next((run_dir / "samples").glob("*/*"))
            wire_trace = sample / "wire_trace.jsonl"
            wire_trace.unlink()

            result = Collector(
                CollectionConfig(
                    run_dir=run_dir,
                    resume=True,
                    keep_aux_traces=True,
                ),
                lambda slot: FakeHarness(state, slot),
            ).collect(requests)

            self.assertEqual(result[0]["status"], "ok")
            self.assertEqual(state["calls"][("task-0", "provider/model")], 2)
            self.assertTrue(wire_trace.is_file())

    def test_resume_reruns_recovery_sample_when_artifact_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            state = self._state()
            state["signature_recovery"] = True
            requests = make_requests()[:1]
            Collector(
                CollectionConfig(run_dir=run_dir),
                lambda slot: FakeHarness(state, slot),
            ).collect(requests)
            sample = next((run_dir / "samples").glob("*/*"))
            recovery = sample / "signature_recovery.jsonl"
            recovery.unlink()

            result = Collector(
                CollectionConfig(run_dir=run_dir, resume=True),
                lambda slot: FakeHarness(state, slot),
            ).collect(requests)

            self.assertEqual(result[0]["status"], "ok")
            self.assertEqual(state["calls"][("task-0", "provider/model")], 2)
            self.assertTrue(recovery.is_file())

    def test_interruption_checkpoints_completed_samples_and_partial_sft(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            state = self._state()
            state["interrupt_task"] = "task-1"
            collector = Collector(
                CollectionConfig(run_dir=run_dir),
                lambda slot: FakeHarness(state, slot),
            )

            with self.assertRaises(KeyboardInterrupt):
                collector.collect(make_requests()[:2])

            info = json.loads((run_dir / "info.json").read_text())
            self.assertEqual(info["state"], "interrupted")
            self.assertEqual(info["summary"]["total"], 2)
            self.assertEqual(info["summary"]["processed"], 1)
            self.assertEqual(info["summary"]["pending"], 1)
            rows = (run_dir / "sft.jsonl").read_text().splitlines()
            self.assertEqual(len(rows), 1)

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
