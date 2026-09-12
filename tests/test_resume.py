from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from roller.actf import convert_openclaw_trace
from roller.collector import CollectionConfig, Collector
from roller.conversation import ConversationManager
from roller.harnesses.base import InteractiveHarness, InteractiveSession, TurnResult
from roller.harness_registry import HARNESS_NAMES as HARNESSES
from roller.models import HarnessResult, RunRequest, Task, UserSimulatorSpec


class RecordingSession(InteractiveSession):
    def __init__(self, harness, request, output_dir) -> None:
        self.harness = harness
        self.request = request
        self.output_dir = output_dir
        self.messages = []
        self.replies = []

    def start(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.harness.calls.append((self.harness.name, self.request.task.id))

    def send(self, message: str, timeout_seconds: float) -> TurnResult:
        if self.request.task.id.endswith("__user_simulator"):
            reply = (
                '{"action":"message","content":"say done"}'
                if not self.messages else '{"action":"finish"}'
            )
        else:
            reply = "done"
        self.messages.append(message)
        self.replies.append(reply)
        return TurnResult(reply, 0.01)

    def finish(self, status: str, error: str | None = None) -> HarnessResult:
        events = []
        for message, reply in zip(self.messages, self.replies):
            events.extend([
                {"type": "message", "message": {"role": "user", "content": message}},
                {"type": "message", "message": {"role": "assistant", "content": reply}},
            ])
        raw = self.output_dir / "trace.jsonl"
        raw.write_text("\n".join(json.dumps(row) for row in events) + "\n")
        actf = self.output_dir / "trace_ACTF.json"
        convert_openclaw_trace(raw, actf, user_messages=self.messages)
        (self.output_dir / "wire_trace.jsonl").write_text("{}\n")
        # Match actual artifact availability: the other clients emit no cache trace.
        if self.harness.name == "openclaw" and self.harness.capture_cache:
            (self.output_dir / "cache_trace.jsonl").write_text("{}\n")
        return HarnessResult(
            status=status, error=error, elapsed_seconds=0.01,
            trace_path=str(raw), trajectory_path=str(actf),
        )


class RecordingHarness(InteractiveHarness):
    def __init__(self, name, calls, capture_cache=True) -> None:
        self.name = name
        self.calls = calls
        self.capture_cache = capture_cache

    def create_session(self, request, output_dir, attempt, *, collect_outputs=True):
        return RecordingSession(self, request, output_dir)

    def run(self, request, output_dir, attempt):
        session = self.create_session(request, output_dir, attempt)
        session.start()
        for prompt in request.task.prompts:
            session.send(prompt, request.timeout_seconds)
        return session.finish("ok")


class MixedResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.calls = []
        self.keep_aux = True
        self.capture_cache = True
        self.override = None
        self.requests = []
        for agent in HARNESSES:
            for simulator in (None, *HARNESSES):
                index = len(self.requests)
                task = Task(
                    id=f"{agent}--{simulator or 'predefined'}",
                    prompts=() if simulator else ("say done",),
                    source_index=index, harness=agent,
                    user_simulator=(
                        UserSimulatorSpec("ask once", harness=simulator, max_turns=1)
                        if simulator else None
                    ),
                )
                self.requests.append(RunRequest(index, task, "provider/model", 30))

    def factory(self, slot, request):
        agent = RecordingHarness(
            self.override or request.task.harness, self.calls, self.capture_cache
        )
        spec = request.task.user_simulator
        if spec is None:
            return agent
        simulator = RecordingHarness(spec.harness, self.calls, self.capture_cache)
        return ConversationManager(agent, simulator, keep_aux_traces=self.keep_aux)

    def collector(self, resume=False):
        return Collector(CollectionConfig(
            run_dir=self.root, resume=resume, keep_aux_traces=self.keep_aux,
            capture_cache_trace=self.capture_cache,
        ), self.factory)

    def collect(self, resume=False):
        return self.collector(resume).collect(self.requests)

    def participants(self):
        for request in self.requests:
            sample = self.collector()._sample_dir(request)
            yield request, sample, self.override or request.task.harness
            if request.task.user_simulator:
                yield request, sample / "user_simulator", request.task.user_simulator.harness

    def assert_reused(self):
        before = list(self.calls)
        raw_before = {
            str(path): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.root.glob("samples/*/*/**/trace.jsonl")
        }
        self.assertEqual([r["status"] for r in self.collect(True)], ["skipped"] * len(self.requests))
        self.assertEqual(self.calls, before)
        for name, snapshot in raw_before.items():
            path = Path(name)
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), snapshot)

    def assert_only_rerun(self, request):
        before = len(self.calls)
        results = self.collect(True)
        for result in results:
            expected = "ok" if result["task_id"] == request.task.id else "skipped"
            self.assertEqual(result["status"], expected)
        expected_calls = 2 if request.task.user_simulator else 1
        self.assertEqual(len(self.calls) - before, expected_calls)

    def test_full_mode_reuses_all_agents_and_simulator_pairs(self):
        self.assertEqual([r["status"] for r in self.collect()], ["ok"] * len(self.requests))
        for _, path, harness in self.participants():
            self.assertTrue((path / "wire_trace.jsonl").is_file())
            self.assertEqual((path / "cache_trace.jsonl").is_file(), harness == "openclaw")
        self.assert_reused()

    def test_missing_openclaw_cache_reruns_only_its_sample(self):
        self.collect()
        for request, path, harness in self.participants():
            if harness != "openclaw":
                continue
            with self.subTest(task=request.task.id, participant=path.name):
                cache = path / "cache_trace.jsonl"
                cache.unlink()
                self.assert_only_rerun(request)
                self.assertTrue(cache.is_file())

    def test_missing_wire_trace_still_reruns_each_harness_on_either_side(self):
        self.collect()
        for request, path, _ in self.participants():
            with self.subTest(task=request.task.id, participant=path.name):
                wire = path / "wire_trace.jsonl"
                wire.unlink()
                self.assert_only_rerun(request)
                self.assertTrue(wire.is_file())

    def test_corrupt_sft_still_reruns_each_harness_on_either_side(self):
        self.collect()
        for request, path, _ in self.participants():
            with self.subTest(task=request.task.id, participant=path.name):
                (path / "trace_SFT.json").write_text('{"messages":[],"tools":[]}\n')
                self.assert_only_rerun(request)

    def test_disabled_or_pruned_cache_traces_do_not_block_resume(self):
        for self.keep_aux, self.capture_cache in ((True, False), (False, True)):
            with self.subTest(full=self.keep_aux, cache=self.capture_cache):
                self.collect()
                for _, path, _ in self.participants():
                    self.assertFalse((path / "cache_trace.jsonl").exists())
                self.assert_reused()

    def test_executed_agent_harness_takes_precedence_over_task_declaration(self):
        for self.override in HARNESSES:
            with self.subTest(override=self.override):
                self.collect()
                self.assert_reused()
                if self.override == "openclaw":
                    request = next(r for r in self.requests if r.task.harness == "codex")
                    (self.collector()._sample_dir(request) / "cache_trace.jsonl").unlink()
                    self.assert_only_rerun(request)

    def test_missing_or_unknown_harness_metadata_keeps_strict_cache_check(self):
        self.requests = self.requests[:1]
        for harness in (None, "unknown"):
            with self.subTest(harness=harness):
                self.collect()
                sample = self.collector()._sample_dir(self.requests[0])
                info_path = sample / "info.json"
                info = json.loads(info_path.read_text())
                if harness is None:
                    del info["harness"]
                else:
                    info["harness"] = harness
                info_path.write_text(json.dumps(info))
                (sample / "cache_trace.jsonl").unlink()
                self.assert_only_rerun(self.requests[0])


if __name__ == "__main__":
    unittest.main()
