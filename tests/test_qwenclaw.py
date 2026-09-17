from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import patch

from roller.benches.base import (
    BenchBinding,
    BenchFormatError,
    GradeSpec,
    binding_from_task_row,
)
from roller.benches.grading import GradeOutcome
from roller.benches.qwenclaw import (
    OUTPUT_DIR_NAME,
    QwenClawBenchAdapter,
    QwenClawGradedHarness,
    build_grader_source,
    parse_task_markdown,
)
from roller.benches.transcripts import grader_messages
from roller.harnesses.hermes import HermesConfig, HermesHarness


def _task_markdown(
    *,
    task_id: str = "task_00001_demo",
    destination: str = "nested/input.txt",
    automated_score: float = 1.0,
) -> str:
    return f"""\
---
id: {task_id}
name: Demo task
category: Files
subcategory: Basic
grading_type: hybrid
grading_weights:
  automated: 0.4
  llm_judge: 0.6
timeout_seconds: 60
workspace_files:
  - source: seed.txt
    dest: {destination}
  - content: inline value
    dest: inline.txt
---
## Prompt
Update the supplied files.

## Expected Behavior
The requested update is completed.

## Grading Criteria
- [ ] The files were updated correctly.

## Automated Checks
```python
def grade(transcript, workspace_path):
    return {{"file_check": {automated_score}}}
```

## LLM Judge Rubric
- Judge whether the requested update was completed.
"""


def _write_dataset(
    root: Path,
    *,
    markdown: str | None = None,
    task_id: str = "task_00001_demo",
) -> Path:
    tasks = root / "tasks"
    assets = root / "assets" / task_id
    tasks.mkdir(parents=True)
    assets.mkdir(parents=True)
    task_path = tasks / f"{task_id}.md"
    task_path.write_text(markdown or _task_markdown(task_id=task_id), encoding="utf-8")
    (assets / "seed.txt").write_text("seed value\n", encoding="utf-8")
    return task_path


class _JudgeHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, Any]]] = []
    judge_score = 0.8

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.__class__.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )
        content = json.dumps(
            {
                "scores": {"completion": self.__class__.judge_score},
                "total": self.__class__.judge_score,
                "notes": "fake judge",
            }
        )
        payload = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": content}}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        return


class QwenClawWorkspaceTests(unittest.TestCase):
    def test_load_maps_sources_and_inline_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_dataset(root)

            rows = QwenClawBenchAdapter(cache_root=root / "cache").load(root)

            self.assertEqual(len(rows), 1)
            workspace = Path(rows[0]["workspace"])
            self.assertEqual(
                (workspace / "nested" / "input.txt").read_text(encoding="utf-8"),
                "seed value\n",
            )
            self.assertEqual(
                (workspace / "inline.txt").read_text(encoding="utf-8"),
                "inline value",
            )
            binding = binding_from_task_row(rows[0])
            self.assertIsNotNone(binding)
            assert binding is not None
            self.assertEqual(binding.metadata["upstream_task_id"], "task_00001_demo")
            self.assertEqual(binding.grade.workspace_path, "/home/node/workspace")

    def test_rejects_destination_that_escapes_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_dataset(root, markdown=_task_markdown(destination="../escaped.txt"))

            with self.assertRaisesRegex(BenchFormatError, "unsafe path"):
                QwenClawBenchAdapter(cache_root=root / "cache").load(root)

    def test_rejects_task_id_that_escapes_cache_root(self) -> None:
        with self.assertRaisesRegex(BenchFormatError, "unsafe task id"):
            parse_task_markdown(
                _task_markdown(task_id="../escape"),
                "unsafe-task.md",
            )

    def test_unknown_task_id_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_dataset(root)

            with self.assertRaisesRegex(
                BenchFormatError, "unknown QwenClawBench task ids: missing"
            ):
                QwenClawBenchAdapter(cache_root=root / "cache").load(
                    root, task_ids=["missing"]
                )


class QwenClawGraderTests(unittest.TestCase):
    def setUp(self) -> None:
        _JudgeHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _JudgeHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _judge_env(self) -> dict[str, str]:
        return {
            "QWENCLAW_JUDGE_BASE_URL": (
                f"http://127.0.0.1:{self.server.server_port}/v1"
            ),
            "QWENCLAW_JUDGE_API_KEY": "judge-secret",
            "QWENCLAW_JUDGE_MODEL": "provider/qwen-local",
            "QWENCLAW_JUDGE_MAX_RETRIES": "1",
        }

    def _run_grader(
        self,
        task_path: Path,
        workspace: Path,
        transcript: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        task = parse_task_markdown(
            task_path.read_text(encoding="utf-8"),
            str(task_path),
            source_path=task_path,
        )
        namespace: dict[str, Any] = {}
        exec(build_grader_source(task), namespace)  # noqa: S102
        with patch.dict(os.environ, self._judge_env()):
            return namespace["roller_grade"](transcript or [], str(workspace))

    def test_removes_unused_yaml_import_from_automated_grader(self) -> None:
        markdown = _task_markdown().replace(
            "def grade(transcript, workspace_path):\n"
            "    return {\"file_check\": 1.0}",
            "def grade(transcript, workspace_path):\n"
            "    import os, re, yaml\n"
            "    return {\"file_check\": 1.0}",
        )
        task = parse_task_markdown(markdown, "unused-yaml.md")

        source = build_grader_source(task)

        self.assertIn("import os, re", source)
        self.assertNotIn("import os, re, yaml", source)

    def test_keeps_yaml_import_when_automated_grader_uses_it(self) -> None:
        markdown = _task_markdown().replace(
            "def grade(transcript, workspace_path):\n"
            "    return {\"file_check\": 1.0}",
            "def grade(transcript, workspace_path):\n"
            "    import yaml\n"
            "    return {\"file_check\": float(bool(yaml.safe_load(\"ok: true\")))}",
        )
        task = parse_task_markdown(markdown, "used-yaml.md")

        source = build_grader_source(task)

        self.assertIn("import yaml", source)

    def test_hybrid_grader_uses_openai_contract_and_snapshots_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_path = _write_dataset(root)
            row = QwenClawBenchAdapter(cache_root=root / "cache").load(root)[0]
            workspace = Path(row["workspace"])

            transcript = [
                {
                    "type": "message",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Update the report."}],
                    },
                },
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "Inspect actual files."},
                            {"type": "text", "text": "The report is complete."},
                        ],
                    },
                },
                {
                    "type": "message",
                    "message": {
                        "role": "toolResult",
                        "content": [{"type": "text", "text": "report.txt"}],
                    },
                },
            ]
            scores = self._run_grader(task_path, workspace, transcript)

            self.assertAlmostEqual(scores["automated_score"], 1.0)
            self.assertAlmostEqual(scores["llm_judge_score"], 0.8)
            self.assertAlmostEqual(scores["score_simple"], 0.88)
            self.assertAlmostEqual(scores["overall_score"], 0.88)
            self.assertEqual(
                (workspace / OUTPUT_DIR_NAME / "nested" / "input.txt").read_text(
                    encoding="utf-8"
                ),
                "seed value\n",
            )
            self.assertEqual(len(_JudgeHandler.requests), 1)
            request = _JudgeHandler.requests[0]
            self.assertEqual(request["path"], "/v1/chat/completions")
            self.assertEqual(request["authorization"], "Bearer judge-secret")
            self.assertEqual(request["body"]["model"], "qwen-local")
            judge_prompt = request["body"]["messages"][0]["content"]
            self.assertIn("User: Update the report.", judge_prompt)
            self.assertIn("Reasoning: Inspect actual files.", judge_prompt)
            self.assertIn("Assistant: The report is complete.", judge_prompt)
            self.assertIn("Result: report.txt", judge_prompt)

    def test_hybrid_penalty_discards_judge_score_below_automated_threshold(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_path = _write_dataset(
                root, markdown=_task_markdown(automated_score=0.5)
            )
            task = parse_task_markdown(
                task_path.read_text(encoding="utf-8"),
                str(task_path),
                source_path=task_path,
            )
            workspace = root / "workspace"
            workspace.mkdir()
            namespace: dict[str, Any] = {}
            exec(build_grader_source(task), namespace)  # noqa: S102

            with patch.dict(os.environ, self._judge_env()):
                scores = namespace["roller_grade"]([], str(workspace))

            self.assertAlmostEqual(scores["score_simple"], 0.68)
            self.assertAlmostEqual(scores["overall_score"], 0.2)


class QwenClawAggregateTests(unittest.TestCase):
    def test_aggregate_counts_failed_rollouts_and_weights_upstream_tasks(self) -> None:
        rows = [
            {
                "bench_metadata": {"upstream_task_id": "task_a"},
                "bench_scores": {"overall_score": 1.0},
                "bench_score_key": "overall_score",
            },
            {
                "bench_metadata": {"upstream_task_id": "task_a"},
                "bench_scores": None,
                "bench_score_key": "overall_score",
                "status": "error",
            },
        ]
        rows.extend(
            {
                "bench_metadata": {"upstream_task_id": "task_b"},
                "bench_scores": {"overall_score": 1.0},
                "bench_score_key": "overall_score",
            }
            for _ in range(3)
        )

        report = QwenClawBenchAdapter().aggregate(rows)

        self.assertEqual(report["average"], 0.75)
        self.assertEqual(report["scored_count"], 4)
        self.assertEqual(report["unscored_count"], 1)
        self.assertEqual(report["upstream_task_count"], 2)
        self.assertEqual(report["perfect_task_count"], 1)
        self.assertEqual(report["strict_accuracy"], 0.5)
        self.assertEqual(report["runs_per_task"], {"task_a": 2, "task_b": 3})


class QwenClawTranscriptTests(unittest.TestCase):
    def test_normalizes_actf_tool_calls_for_upstream_graders(self) -> None:
        events = grader_messages(
            {
                "steps": [
                    {
                        "user_content": "Inspect downloads.",
                        "assistant_content": {
                            "content": "Done.",
                            "reasoning_content": "Check actual sizes.",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "name": "exec",
                                    "arguments": {"command": "ls -l downloads"},
                                }
                            ],
                        },
                        "metric": {"stop_reason": "toolUse"},
                        "observation": [
                            {
                                "role": "toolResult",
                                "toolCallId": "call-1",
                                "toolName": "exec",
                                "content": [{"type": "text", "text": "report.csv"}],
                            }
                        ],
                    }
                ]
            }
        )

        tool = events[1]["message"]["content"][2]
        self.assertEqual(tool["type"], "tool_use")
        self.assertEqual(tool["input"], {"command": "ls -l downloads"})
        self.assertEqual(events[2]["message"]["toolCallId"], "call-1")

    def test_normalized_transcript_is_staged_for_container_grader(self) -> None:
        class RecordingRuntime:
            def __init__(self) -> None:
                self.staged: list[dict[str, Any]] | None = None

            def run(self, args, timeout=None):
                del timeout
                arguments = list(args)
                if arguments[0] == "cp":
                    self.staged = json.loads(Path(arguments[1]).read_text())
                return SimpleNamespace(returncode=0, stdout="", stderr="")

        runtime = RecordingRuntime()
        inner = HermesHarness(
            HermesConfig(docker_image="registry.example/hermes:test"),
            1,
            runtime,
        )
        binding = BenchBinding(
            name="qwenclawbench",
            grade=GradeSpec(
                code="def roller_grade(transcript, workspace_path): return {}",
                needs_transcript=True,
                transcript_path="/tmp/roller-qwenclaw-transcript.json",
            ),
        )
        harness = QwenClawGradedHarness(inner, binding)
        session = SimpleNamespace(
            container_started=True,
            container="agent-container",
            harness=inner,
        )
        request = SimpleNamespace(task=SimpleNamespace(id="task-test"))
        expected = GradeOutcome({"overall_score": 1.0}, 0.1)
        trajectory = {
            "steps": [
                {
                    "user_content": "Inspect files.",
                    "assistant_content": {
                        "content": "Done.",
                        "reasoning_content": "",
                        "tool_calls": [],
                    },
                    "metric": {"stop_reason": "stop"},
                    "observation": [],
                }
            ]
        }

        with patch(
            "roller.benches.qwenclaw.session_trajectory",
            return_value=trajectory,
        ):
            with patch(
                "roller.benches.grading.ContainerGrader.grade",
                return_value=expected,
            ) as grade:
                outcome = harness._grade(session, request, "ok")

        self.assertIs(outcome, expected)
        self.assertEqual(runtime.staged, grader_messages(trajectory))
        self.assertEqual(
            grade.call_args.kwargs["transcript_container_path"],
            "/tmp/roller-qwenclaw-transcript.json",
        )


if __name__ == "__main__":
    unittest.main()
