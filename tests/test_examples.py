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
        tasks = load_tasks(self.examples_root / "tasks" / "tasks.jsonl")

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
        self.assertEqual(len(tasks[0].inputs), 0)
        self.assertEqual(tasks[1].inputs[0].path, "inputs/product-brief.md")
        self.assertEqual(
            tasks[1].inputs[0].source,
            self.examples_root
            / "tasks/files/02_local_markdown/inputs/product-brief.md",
        )
        self.assertEqual(
            [item.role for item in tasks[2].context_files], ["user", "memory"]
        )
        self.assertEqual(tasks[1].output_dir, "outputs")

        with tempfile.TemporaryDirectory() as temporary:
            bundles = TaskBundleStore(Path(temporary)).prepare_all(tasks)
            markdown_bundle = bundles["02_local_markdown"]
            persona_bundle = bundles["03_user_memory_context"]
            self.assertTrue(
                (markdown_bundle.inputs_root / "inputs/product-brief.md").is_file()
            )
            self.assertTrue((persona_bundle.context_root / "USER.md").is_file())
            self.assertTrue((persona_bundle.context_root / "MEMORY.md").is_file())

    def test_custom_api_example_expands_environment_placeholders(self) -> None:
        config = load_model_config(
            self.examples_root / "json" / "openclaw-models.json",
            {
                "MY_PROXY_BASE_URL": "https://proxy.example/v1",
                "MY_PROXY_API_KEY": "secret",
            },
        )

        provider = config["providers"]["my-openai-proxy"]
        self.assertEqual(provider["baseUrl"], "https://proxy.example/v1")
        self.assertEqual(provider["apiKey"], "secret")
        self.assertEqual(provider["api"], "openai-responses")
        self.assertEqual(
            provider["models"],
            [{"id": "my-model", "name": "My model"}],
        )

        multimodal = load_model_config(
            self.examples_root / "json" / "openclaw-models-multimodal.json",
            {
                "MY_PROXY_BASE_URL": "https://proxy.example/v1",
                "MY_PROXY_API_KEY": "secret",
            },
        )
        multimodal_model = multimodal["providers"]["my-openai-proxy"]["models"][0]
        self.assertEqual(multimodal_model["id"], "my-multimodal-model")
        self.assertEqual(multimodal_model["input"], ["text", "image"])

    def test_user_simulator_example_is_an_independent_dynamic_task(self) -> None:
        tasks = load_tasks(
            self.examples_root / "tasks" / "user-simulator.jsonl"
        )
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task.prompts, ())
        self.assertIsNotNone(task.user_simulator)
        assert task.user_simulator is not None
        self.assertEqual(task.user_simulator.harness, "openclaw")
        self.assertEqual(task.user_simulator.max_turns, 6)
        self.assertIn("HR", task.user_simulator.instruction)

    def test_environment_example_includes_model_and_search_variables(self) -> None:
        content = (self.examples_root / "env" / "api.env.example").read_text()
        self.assertIn("MY_PROXY_BASE_URL=https://api.example.com/v1", content)
        self.assertIn("MY_PROXY_API_KEY=replace-with-your-api-key", content)
        self.assertIn("BRAVE_API_KEY=replace-with-your-brave-api-key", content)


if __name__ == "__main__":
    unittest.main()
