from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from roller.tasks import TaskFormatError, build_requests, load_tasks


class TaskLoadingTests(unittest.TestCase):
    def test_normalizes_and_validates_task_and_simulator_thinking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_file = Path(temporary) / "tasks.json"
            task_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "reasoning",
                            "model": "provider/model",
                            "runtime": {"thinking": "none"},
                            "user_simulator": {
                                "instruction": "continue if needed",
                                "thinking": "xhigh",
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )

            task = load_tasks(task_file)[0]
            self.assertEqual(task.thinking, "off")
            assert task.user_simulator is not None
            self.assertEqual(task.user_simulator.thinking, "xhigh")

            payload = json.loads(task_file.read_text(encoding="utf-8"))
            payload[0]["runtime"]["thinking"] = "turbo"
            task_file.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(TaskFormatError, "runtime.thinking must be one of"):
                load_tasks(task_file)

    def test_loads_user_simulator_task_without_predefined_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_file = Path(temporary) / "tasks.json"
            task_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "simulated",
                            "model": "provider/agent",
                            "user_simulator": {
                                "instruction": "You are an HR manager.",
                                "model": "provider/simulator",
                                "max_turns": 5,
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )
            task = load_tasks(task_file)[0]

            self.assertEqual(task.prompts, ())
            self.assertIsNotNone(task.user_simulator)
            assert task.user_simulator is not None
            self.assertEqual(task.user_simulator.model, "provider/simulator")
            self.assertEqual(task.user_simulator.max_turns, 5)
            self.assertEqual(build_requests([task], [], 30)[0].model, "provider/agent")

            payload = json.loads(task_file.read_text())
            payload[0]["prompt"] = "conflict"
            task_file.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(TaskFormatError, "cannot be combined"):
                load_tasks(task_file)

    def test_task_agent_and_simulator_harnesses_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_file = Path(temporary) / "tasks.jsonl"
            task_file.write_text(
                json.dumps(
                    {
                        "id": "mixed-harness",
                        "model": "provider/agent",
                        "harness": "codex",
                        "user_simulator": {
                            "instruction": "continue",
                            "harness": "claude-code",
                            "model": "provider/simulator",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            task = load_tasks(task_file)[0]
            assert task.user_simulator is not None
            self.assertEqual(task.harness, "codex")
            self.assertEqual(task.user_simulator.harness, "claude-code")

    def test_agent_harness_alias_must_not_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_file = Path(temporary) / "tasks.jsonl"
            task_file.write_text(
                json.dumps(
                    {
                        "id": "conflict",
                        "prompt": "hello",
                        "harness": "codex",
                        "agent_harness": "openclaw",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TaskFormatError, "must match"):
                load_tasks(task_file)

    def test_loads_interns_question_and_multi_turn_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            task_file = root / "tasks.jsonl"
            rows = [
                {"id": "one", "question": "research this", "workspace": "workspace"},
                {
                    "id": "two",
                    "messages": [
                        {"role": "user", "content": "first"},
                        {"role": "user", "content": "second"},
                    ],
                    "model": "provider/per-task",
                    "env": "TOKEN_A\nTOKEN_B",
                    "setup": ["echo ready"],
                },
            ]
            task_file.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
            )

            tasks = load_tasks(task_file)

            self.assertEqual(tasks[0].prompts, ("research this",))
            self.assertEqual(tasks[0].workspace, workspace.resolve())
            self.assertEqual(tasks[1].prompts, ("first", "second"))
            self.assertEqual(tasks[1].env_names, ("TOKEN_A", "TOKEN_B"))
            self.assertEqual(tasks[1].setup_commands, ("echo ready",))

    def test_models_create_a_cross_product(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_file = Path(temporary) / "tasks.json"
            task_file.write_text(
                json.dumps(
                    [
                        {"id": "one", "prompt": "a"},
                        {"id": "two", "prompt": "b"},
                    ]
                ),
                encoding="utf-8",
            )
            tasks = load_tasks(task_file)
            requests = build_requests(tasks, ["provider/a", "provider/b"], 60)

            self.assertEqual(len(requests), 4)
            self.assertEqual(
                [(request.task.id, request.model) for request in requests],
                [
                    ("one", "provider/a"),
                    ("one", "provider/b"),
                    ("two", "provider/a"),
                    ("two", "provider/b"),
                ],
            )

    def test_samples_repeat_each_task_model_with_stable_task_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_file = Path(temporary) / "tasks.json"
            task_file.write_text(
                json.dumps([{"id": "one", "prompt": "a"}]), encoding="utf-8"
            )
            tasks = load_tasks(task_file)
            requests = build_requests(tasks, ["provider/a", "provider/b"], 60, 2)

            self.assertEqual(len(requests), 4)
            self.assertEqual([request.index for request in requests], list(range(4)))
            self.assertEqual(
                [
                    (
                        request.task.id,
                        request.model,
                        request.sample_index,
                        request.sample_count,
                    )
                    for request in requests
                ],
                [
                    ("one", "provider/a", 0, 2),
                    ("one", "provider/a", 1, 2),
                    ("one", "provider/b", 0, 2),
                    ("one", "provider/b", 1, 2),
                ],
            )
            with self.assertRaisesRegex(ValueError, "samples_per_task"):
                build_requests(tasks, ["provider/a"], 60, 0)

    def test_resolves_sources_against_task_file_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            manifests = project / "manifests"
            files = project / "files"
            manifests.mkdir(parents=True)
            files.mkdir()
            source = files / "input.md"
            source.write_text("input", encoding="utf-8")
            task_file = manifests / "tasks.jsonl"
            task_file.write_text(
                json.dumps(
                    {
                        "id": "rooted",
                        "file_root": "..",
                        "prompt": "read input",
                        "files": {"inputs": [{"source": "files/input.md"}]},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            task = load_tasks(task_file)[0]

            self.assertEqual(task.inputs[0].source, source)
            self.assertEqual(task.inputs[0].path, "files/input.md")

    def test_rejects_duplicate_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_file = Path(temporary) / "tasks.json"
            task_file.write_text(
                json.dumps(
                    [
                        {"id": "same", "prompt": "a"},
                        {"id": "same", "prompt": "b"},
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TaskFormatError, "duplicate task id"):
                load_tasks(task_file)

    def test_accepts_custom_output_directory_and_rejects_reserved_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_file = Path(temporary) / "tasks.json"
            task_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "custom",
                            "prompt": "write a file",
                            "files": {
                                "output_dir": "deliverables/final",
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )
            task = load_tasks(task_file)[0]
            self.assertEqual(task.output_dir, "deliverables/final")

            task_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "bad",
                            "prompt": "write a file",
                            "files": {"output_dir": "inputs/generated"},
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TaskFormatError, "reserved"):
                load_tasks(task_file)

    def test_accepts_renamed_and_inline_inputs_but_rejects_output_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("input", encoding="utf-8")
            task_file = root / "tasks.json"
            task_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "target",
                            "prompt": "read",
                            "files": {
                                "inputs": [
                                    {"source": "input.txt", "target": "renamed.txt"},
                                    {"path": "inline/config.yaml", "content": "ok: true\n"},
                                ],
                                "input_mode": "copy",
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )
            task = load_tasks(task_file)[0]
            self.assertEqual(task.input_mode, "copy")
            self.assertEqual(task.inputs[0].source, root / "input.txt")
            self.assertEqual(task.inputs[0].path, "renamed.txt")
            self.assertIsNone(task.inputs[1].source)
            self.assertEqual(task.inputs[1].path, "inline/config.yaml")
            self.assertEqual(task.inputs[1].content, "ok: true\n")

            task_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "outputs",
                            "prompt": "write",
                            "files": {"outputs": ["answer.md"]},
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TaskFormatError, "files.outputs.*no longer supported"):
                load_tasks(task_file)

    def test_rejects_invalid_input_mode_and_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("input", encoding="utf-8")
            task_file = root / "tasks.json"
            task_file.write_text(
                json.dumps(
                    [{
                        "id": "bad-mode",
                        "prompt": "read",
                        "files": {"inputs": ["input.txt"], "input_mode": "move"},
                    }]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TaskFormatError, "input_mode"):
                load_tasks(task_file)

            task_file.write_text(
                json.dumps(
                    [{
                        "id": "bad-target",
                        "prompt": "read",
                        "files": {
                            "inputs": [
                                {"source": "input.txt", "target": "../outside.txt"}
                            ]
                        },
                    }]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TaskFormatError, "parent traversal"):
                load_tasks(task_file)

    def test_loads_fresh_session_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_file = Path(temporary) / "tasks.json"
            task_file.write_text(
                json.dumps(
                    [{
                        "id": "sessions",
                        "messages": [
                            {"role": "user", "content": "first"},
                            {
                                "role": "user",
                                "content": "second",
                                "new_session": True,
                            },
                        ],
                    }]
                ),
                encoding="utf-8",
            )
            task = load_tasks(task_file)[0]
            self.assertEqual(task.prompts, ("first", "second"))
            self.assertEqual(task.new_session, (False, True))

    def test_rejects_input_outside_file_root_and_output_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "inputs").mkdir()
            (root / "inputs" / "input.txt").write_text("input", encoding="utf-8")
            (root / "shared.txt").write_text("shared", encoding="utf-8")
            task_file = root / "tasks.json"
            task_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "outside",
                            "prompt": "read",
                            "files": {"inputs": ["../outside.txt"]},
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TaskFormatError, "parent traversal"):
                load_tasks(task_file)

            task_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "overlap",
                            "prompt": "write",
                            "files": {
                                "inputs": ["inputs"],
                                "output_dir": "inputs/generated",
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TaskFormatError, "reserved|overlaps input"):
                load_tasks(task_file)

            task_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "context-overlap",
                            "prompt": "read",
                            "files": {
                                "inputs": ["shared.txt"],
                                "context": [
                                    {
                                        "role": "custom",
                                        "source": "shared.txt",
                                        "target": "shared.txt",
                                    }
                                ],
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TaskFormatError, "overlaps context target"):
                load_tasks(task_file)


if __name__ == "__main__":
    unittest.main()
