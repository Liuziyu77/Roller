from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from roller.bundles import TaskBundleStore
from roller.harnesses.openclaw import load_model_config
from roller.tasks import load_tasks


class ExampleFilesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project_root = Path(__file__).resolve().parents[1]
        cls.examples_root = cls.project_root / "examples"

    def test_four_example_tasks_and_their_input_bundle_are_valid(self) -> None:
        tasks = load_tasks(self.examples_root / "tasks.jsonl")

        self.assertEqual(
            [task.id for task in tasks],
            [
                "01_text_only",
                "02_local_markdown",
                "03_user_memory_context",
                "04_multi_turn",
            ],
        )
        self.assertEqual([len(task.prompts) for task in tasks], [1, 1, 1, 2])
        self.assertTrue(all(task.raw["file_root"] == ".." for task in tasks))
        self.assertEqual(len(tasks[0].inputs), 0)
        self.assertEqual(tasks[1].inputs[0].target, "product-brief.md")
        self.assertEqual(
            tasks[1].inputs[0].source,
            self.examples_root / "task_files/02_local_markdown/product-brief.md",
        )
        self.assertEqual(
            [item.role for item in tasks[2].context_files], ["user", "memory"]
        )
        self.assertEqual([len(task.outputs) for task in tasks], [0, 1, 0, 0])
        self.assertEqual(tasks[1].output_dir, "quick_results")

        with tempfile.TemporaryDirectory() as temporary:
            bundles = TaskBundleStore(Path(temporary)).prepare_all(tasks)
            markdown_bundle = bundles["02_local_markdown"]
            persona_bundle = bundles["03_user_memory_context"]
            self.assertTrue((markdown_bundle.inputs_root / "product-brief.md").is_file())
            self.assertTrue((persona_bundle.context_root / "USER.md").is_file())
            self.assertTrue((persona_bundle.context_root / "MEMORY.md").is_file())

    def test_custom_api_example_expands_environment_placeholders(self) -> None:
        config = load_model_config(
            self.examples_root / "openclaw-models.json",
            {
                "MY_PROXY_BASE_URL": "https://proxy.example/v1",
                "MY_PROXY_API_KEY": "secret",
            },
        )

        provider = config["providers"]["my-openai-proxy"]
        self.assertEqual(provider["baseUrl"], "https://proxy.example/v1")
        self.assertEqual(provider["apiKey"], "secret")
        self.assertEqual(provider["api"], "openai-responses")


if __name__ == "__main__":
    unittest.main()
