from __future__ import annotations

import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from roller.benches import BENCHES
from roller.benches.run_config import load_bench_run_plan
from roller.harness_registry import HARNESS_NAMES
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
            self.examples_root / "config" / "openclaw" / "models.json",
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
            self.examples_root
            / "config"
            / "openclaw"
            / "models-multimodal.json",
            {
                "MY_PROXY_BASE_URL": "https://proxy.example/v1",
                "MY_PROXY_API_KEY": "secret",
            },
        )
        multimodal_model = multimodal["providers"]["my-openai-proxy"]["models"][0]
        self.assertEqual(multimodal_model["id"], "my-multimodal-model")
        self.assertEqual(multimodal_model["input"], ["text", "image"])

    def test_native_harness_config_examples_are_safe_and_parseable(self) -> None:
        codex_path = (
            self.examples_root / "config" / "codex" / "config.toml.example"
        )
        codex_config = tomllib.loads(codex_path.read_text(encoding="utf-8"))
        self.assertEqual(codex_config, {"model_verbosity": "medium"})

        claude_path = (
            self.examples_root
            / "config"
            / "claude-code"
            / "settings.json.example"
        )
        claude_settings = json.loads(claude_path.read_text(encoding="utf-8"))
        self.assertFalse(claude_settings["includeCoAuthoredBy"])

        combined = codex_path.read_text(encoding="utf-8") + claude_path.read_text(
            encoding="utf-8"
        )
        self.assertNotIn("apiKey", combined)
        self.assertNotIn("API_KEY", combined)
        self.assertNotIn('"hooks"', combined)

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

    def test_benchmark_config_examples_are_layerable_and_secret_free(self) -> None:
        config_root = self.examples_root / "benchmarks"
        environment = {
            "ROLLER_MODEL_ID": "example/model",
            "ROLLER_MODEL_BASE_URL": "https://model.example/v1",
            "ROLLER_MODEL_API_KEY": "model-secret",
            "ROLLER_BENCH_OUTPUT": "/tmp/roller-benchmark-results",
            "ROLLER_JUDGE_BASE_URL": "https://judge.example/v1",
            "ROLLER_JUDGE_API_KEY": "judge-secret",
            "ROLLER_JUDGE_MODEL": "example-judge",
            "ROLLER_SEARCH_API_KEY": "search-secret",
            "WILDCLAWBENCH_ROOT": "/tmp/wildclawbench",
            "PINCHBENCH_ROOT": "/tmp/pinchbench",
            "QWENCLAWBENCH_ROOT": "/tmp/qwenclawbench",
            "SKILLSBENCH_ROOT": "/tmp/skillsbench",
        }
        cases = (
            ("wildclawbench.yaml", (), "wildclawbench", "openclaw"),
            ("pinchbench.yaml", (), "pinchbench", "openclaw"),
            ("knclawbench.yaml", (), "qwenclawbench", "openclaw"),
            ("skillsbench.yaml", (), "skillsbench", "opencode"),
            (
                "skillsbench.yaml",
                ("skillsbench-no-skill.yaml",),
                "skillsbench",
                "opencode",
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            for primary, additional, benchmark, harness in cases:
                with self.subTest(primary=primary, additional=additional):
                    overlays = [str(config_root / primary)] + [
                        str(config_root / name) for name in additional
                    ]
                    plan = load_bench_run_plan(
                        config_root / "base.yaml",
                        overlays,
                        environ=environment,
                        temporary_root=Path(temporary),
                    )
                    argv = list(plan.collect_argv)
                    self.assertEqual(argv[argv.index("--bench") + 1], benchmark)
                    self.assertEqual(argv[argv.index("--harness") + 1], harness)
                    if benchmark == "qwenclawbench":
                        self.assertEqual(
                            argv[argv.index("--bench-runs-per-task") + 1], "3"
                        )
                        self.assertEqual(argv[argv.index("--samples") + 1], "1")
                    self.assertNotIn("model-secret", " ".join(argv))
                    self.assertNotIn("judge-secret", json.dumps(plan.provenance))
                    self.assertNotIn("search-secret", json.dumps(plan.provenance))

        combined = "\n".join(
            path.read_text(encoding="utf-8") for path in config_root.glob("*.yaml")
        )
        self.assertNotIn("sk-", combined)
        self.assertNotIn("joyrouter", combined.lower())

    def test_benchmark_run_schema_tracks_registered_harnesses(self) -> None:
        schema = json.loads(
            (self.project_root / "schemas" / "bench-run.schema.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            "roller.bench-run/v1",
        )
        self.assertEqual(
            set(schema["properties"]["harness"]["properties"]["id"]["enum"]),
            set(HARNESS_NAMES),
        )

    def test_every_registered_benchmark_has_standard_entry_files(self) -> None:
        files = {
            "pinchbench": ("pinchbench.yaml", "prepare-pinchbench.sh"),
            "qwenclawbench": ("knclawbench.yaml", "prepare-knclawbench.sh"),
            "skillsbench": ("skillsbench.yaml", "prepare-skillsbench.sh"),
            "wildclawbench": ("wildclawbench.yaml", "prepare-wildclawbench.sh"),
        }
        self.assertEqual(set(files), set(BENCHES))
        for config_name, script_name in files.values():
            self.assertTrue(
                (self.examples_root / "benchmarks" / config_name).is_file()
            )
            script = self.project_root / "scripts" / script_name
            self.assertTrue(script.is_file())
            self.assertTrue(script.stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
