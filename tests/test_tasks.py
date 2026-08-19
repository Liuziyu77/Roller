from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from roller.tasks import TaskFormatError, build_requests, load_tasks


class TaskLoadingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

