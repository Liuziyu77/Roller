from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from roller.actf import convert_openclaw_trace
from roller.collector import CollectionConfig, Collector
from roller.conversation import (
    ConversationManager,
    build_user_simulator_prompt,
    parse_user_simulator_decision,
)
from roller.harnesses.base import InteractiveHarness, InteractiveSession, TurnResult
from roller.models import HarnessResult, RunRequest, Task, UserSimulatorSpec


class FakeInteractiveSession(InteractiveSession):
    def __init__(self, request, output_dir, responses, sent) -> None:
        self.request = request
        self.output_dir = output_dir
        self.responses = list(responses)
        self.sent = sent
        self.replies: list[str] = []

    def start(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def send(self, message: str, timeout_seconds: float) -> TurnResult:
        self.sent.append(message)
        response = self.responses.pop(0)
        self.replies.append(response)
        return TurnResult(response, 0.01)

    def finish(self, status: str, error: str | None = None) -> HarnessResult:
        rollout = self.output_dir / "rollout"
        rollout.mkdir(parents=True, exist_ok=True)
        trace = rollout / "trace.jsonl"
        events = []
        for index, (message, reply) in enumerate(zip(self.sent, self.replies)):
            events.extend(
                [
                    {
                        "type": "message",
                        "timestamp": 1000 + index * 2,
                        "message": {"role": "user", "content": message},
                    },
                    {
                        "type": "message",
                        "timestamp": 1001 + index * 2,
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": reply}],
                            "usage": {"input": 10, "output": 2, "totalTokens": 12},
                            "stopReason": "stop",
                        },
                    },
                ]
            )
        trace.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )
        trajectory = rollout / "trace_ACTF.json"
        convert_openclaw_trace(
            trace,
            trajectory,
            user_messages=self.sent,
        )
        count = len(self.replies)
        return HarnessResult(
            status=status,
            elapsed_seconds=0.01,
            error=error,
            usage={
                "input_tokens": count * 10,
                "output_tokens": count * 2,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": count * 12,
                "cost_usd": 0.0,
                "request_count": count,
            },
            trace_path="rollout/trace.jsonl",
            trajectory_path="rollout/trace_ACTF.json",
            metadata={"output_collection": None, "provenance": {}},
        )


class FakeInteractiveHarness(InteractiveHarness):
    name = "openclaw"

    def __init__(self, responses: list[str], sent: list[str]) -> None:
        self.responses = responses
        self.sent = sent

    def create_session(
        self,
        request: RunRequest,
        output_dir: Path,
        attempt: int,
        *,
        collect_outputs: bool = True,
    ) -> InteractiveSession:
        return FakeInteractiveSession(request, output_dir, self.responses, self.sent)

    def run(self, request: RunRequest, output_dir: Path, attempt: int) -> HarnessResult:
        raise AssertionError("predefined run path is not used in this test")


class ConversationManagerTests(unittest.TestCase):
    def test_simulator_archive_marks_missing_raw_nontrainable_without_rewriting_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = HarnessResult(
                status="ok",
                elapsed_seconds=0.01,
                usage={},
                trace_path=None,
                metadata={},
            )

            info = ConversationManager._archive_simulator(
                Path(temporary), result, "provider/model", "openclaw", keep_aux_traces=False,
            )
            self.assertEqual(info["status"], "ok")
            self.assertEqual(info["conversion"]["status"], "failed")
            self.assertEqual(info["conversion"]["target"], "raw")
            self.assertFalse(info["training"]["eligible"])

    def test_simulator_archive_applies_actf_trace_options(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            simulator_dir = Path(temporary)
            raw_path = simulator_dir / "trace.jsonl"
            raw_path.write_text(
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {
                            "type": "message",
                            "timestamp": 999,
                            "message": {"role": "user", "content": "prompt"},
                        },
                        {
                            "type": "message",
                            "timestamp": 1000,
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {"type": "thinking", "thinking": "private"},
                                    {"type": "text", "text": "reply"},
                                ],
                            },
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            (simulator_dir / "cache_trace.jsonl").write_text(
                json.dumps({"stage": "stream:context", "system": "sim system"})
                + "\n",
                encoding="utf-8",
            )
            (simulator_dir / "wire_trace.jsonl").write_text(
                json.dumps(
                    {
                        "kind": "tools",
                        "tools": [
                            {"type": "function", "function": {"name": "read"}}
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            result = HarnessResult(
                status="ok",
                elapsed_seconds=0.01,
                usage={},
                trace_path="trace.jsonl",
                trajectory_path="source_ACTF.json",
                metadata={},
            )
            convert_openclaw_trace(
                raw_path,
                simulator_dir / "source_ACTF.json",
                user_messages=["prompt"],
                cache_trace=simulator_dir / "cache_trace.jsonl",
                tools=[{"type": "function", "function": {"name": "read"}}],
                keep_thinking=False,
            )

            ConversationManager._archive_simulator(
                simulator_dir,
                result,
                "provider/model",
                "openclaw",
                keep_aux_traces=False,
            )

            actf = json.loads((simulator_dir / "trace_ACTF.json").read_text())
            self.assertEqual(actf["system_prompt"], "sim system")
            self.assertEqual(actf["tools"][0]["function"]["name"], "read")
            self.assertEqual(
                actf["steps"][0]["assistant_content"]["reasoning_content"], ""
            )
            sft = json.loads((simulator_dir / "trace_SFT.json").read_text())
            self.assertEqual(set(sft), {"messages", "tools"})
            self.assertEqual(sft["messages"][2]["reasoning_content"], "")
            self.assertFalse((simulator_dir / "cache_trace.jsonl").exists())
            self.assertFalse((simulator_dir / "wire_trace.jsonl").exists())

    def test_parses_strict_simulator_json(self) -> None:
        message = parse_user_simulator_decision(
            '{"action":"message","content":"Please revise it."}'
        )
        self.assertEqual(message.content, "Please revise it.")
        finished = parse_user_simulator_decision(
            '{"action":"finish","reason":"The ad is ready."}'
        )
        self.assertEqual(finished.reason, "The ad is ready.")
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            parse_user_simulator_decision("```json\n{}\n```")

    def test_prompt_repeats_fixed_instruction_and_complete_context(self) -> None:
        spec = UserSimulatorSpec("Act as an HR manager", max_turns=4)
        prompt = build_user_simulator_prompt(
            spec,
            [
                {"role": "user", "content": "Draft an ad."},
                {"role": "assistant", "content": "First draft"},
            ],
        )
        self.assertIn("Act as an HR manager", prompt)
        self.assertIn("Draft an ad.", prompt)
        self.assertIn("First draft", prompt)
        self.assertIn('"rounds_completed":1', prompt)

    def test_collector_archives_both_harnesses_and_conversation(self) -> None:
        agent_sent: list[str] = []
        simulator_sent: list[str] = []
        agent = FakeInteractiveHarness(
            ["First recruitment ad", "Revised recruitment ad"], agent_sent
        )
        simulator = FakeInteractiveHarness(
            [
                '{"action":"message","content":"Draft a backend engineer ad."}',
                '{"action":"message","content":"Add salary and remote-work details."}',
                '{"action":"finish","reason":"The recruitment ad is complete."}',
            ],
            simulator_sent,
        )
        task = Task(
            id="hr_ad",
            prompts=(),
            source_index=0,
            user_simulator=UserSimulatorSpec(
                instruction="You are an HR manager creating a recruitment ad.",
                model="provider/simulator",
                max_turns=4,
            ),
        )
        request = RunRequest(0, task, "provider/agent", 30)

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            collector = Collector(
                CollectionConfig(run_dir=run_dir),
                lambda slot: ConversationManager(agent, simulator),
            )
            result = collector.collect([request])[0]
            sample = next((run_dir / "samples").glob("*/*"))
            conversation = json.loads((sample / "conversation.json").read_text())
            info = json.loads((sample / "info.json").read_text())

            self.assertEqual(result["status"], "ok")
            self.assertEqual(agent_sent, [
                "Draft a backend engineer ad.",
                "Add salary and remote-work details.",
            ])
            self.assertEqual(len(simulator_sent), 3)
            self.assertEqual(len(conversation["rounds"]), 2)
            self.assertEqual(
                conversation["termination"]["reason"], "simulator_finished"
            )
            self.assertEqual(info["conversation"]["rounds"], 2)
            self.assertEqual(info["usage"]["request_count"], 5)
            self.assertTrue((sample / "trace.jsonl").is_file())
            self.assertTrue((sample / "trace_ACTF.json").is_file())
            self.assertTrue((sample / "trace_SFT.json").is_file())
            self.assertTrue((sample / "user_simulator" / "trace.jsonl").is_file())
            self.assertTrue(
                (sample / "user_simulator" / "trace_ACTF.json").is_file()
            )
            self.assertTrue(
                (sample / "user_simulator" / "trace_SFT.json").is_file()
            )
            self.assertTrue((sample / "user_simulator" / "info.json").is_file())

    def test_max_turns_records_instruction_that_was_not_delivered(self) -> None:
        agent_sent: list[str] = []
        simulator_sent: list[str] = []
        manager = ConversationManager(
            FakeInteractiveHarness(["draft"], agent_sent),
            FakeInteractiveHarness(
                [
                    '{"action":"message","content":"write a draft"}',
                    '{"action":"message","content":"revise it again"}',
                ],
                simulator_sent,
            ),
        )
        task = Task(
            id="bounded",
            prompts=(),
            source_index=0,
            user_simulator=UserSimulatorSpec("Review the draft", max_turns=1),
        )
        request = RunRequest(0, task, "provider/model", 30)

        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            result = manager.run(request, output_dir, attempt=1)
            conversation = json.loads(
                (output_dir / "conversation.json").read_text()
            )

        self.assertEqual(result.status, "ok")
        self.assertEqual(agent_sent, ["write a draft"])
        self.assertEqual(conversation["termination"]["reason"], "max_turns")
        self.assertEqual(
            conversation["termination"]["pending_instruction"],
            "revise it again",
        )


if __name__ == "__main__":
    unittest.main()
