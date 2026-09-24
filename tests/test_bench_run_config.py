from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from roller.benches.run_config import (
    BENCH_RUN_SCHEMA,
    load_bench_run_plan,
    temporary_environment,
)
from roller.cli import _bench, _build_parser


class BenchRunConfigTests(unittest.TestCase):
    def _write(self, root: Path, name: str, payload: dict) -> Path:
        path = root / name
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        return path

    def _base(self) -> dict:
        return {
            "schema_version": BENCH_RUN_SCHEMA,
            "model": {
                "id": "example/model",
                "base_url": "${MODEL_BASE_URL}",
                "api_key_env": "MODEL_API_KEY",
                "thinking": "high",
            },
            "harness": {"id": "codex"},
            "execution": {
                "parallel": 4,
                "grader_parallel": 2,
                "samples": 1,
                "wire_trace": "actf",
            },
        }

    def test_layers_config_and_maps_common_model_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = self._write(root, "base.yaml", self._base())
            extra = self._write(
                root,
                "bench.yaml",
                {
                    "benchmark": {
                        "id": "wildclawbench",
                        "root": "/bench/wildclaw",
                        "categories": ["03_Social_Interaction"],
                        "timeout_multiplier": 2,
                    },
                    "execution": {"parallel": 8, "run_name": "smoke"},
                },
            )
            plan = load_bench_run_plan(
                base,
                [str(extra)],
                environ={
                    "MODEL_BASE_URL": "https://model.example/v1",
                    "MODEL_API_KEY": "secret-value",
                },
                temporary_root=root,
            )

        argv = list(plan.collect_argv)
        self.assertEqual(argv[argv.index("--parallel") + 1], "8")
        self.assertEqual(argv[argv.index("--grader-parallel") + 1], "2")
        self.assertEqual(argv[argv.index("--api-base") + 1], "https://model.example/v1")
        self.assertEqual(argv[argv.index("--api-key-env") + 1], "MODEL_API_KEY")
        self.assertEqual(argv[argv.index("--thinking") + 1], "high")
        self.assertEqual(
            argv[argv.index("--bench-category") + 1], "03_Social_Interaction"
        )
        self.assertNotIn("secret-value", " ".join(argv))
        self.assertEqual(len(plan.provenance["sources"]), 2)
        effective = plan.provenance["effective"]
        self.assertEqual(effective["model"]["thinking"], "high")
        self.assertEqual(
            effective["model"]["base_url"], "https://model.example/v1"
        )
        self.assertEqual(effective["model"]["api_key_env"], "MODEL_API_KEY")
        self.assertEqual(effective["harness"]["id"], "codex")
        self.assertEqual(effective["execution"]["parallel"], 8)

    def test_openclaw_uses_common_endpoint_to_generate_model_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._base()
            config["benchmark"] = {"id": "pinchbench", "root": "/bench/pinch"}
            config["harness"] = {
                "id": "openclaw",
                "extra": {"model_request_timeout": 900, "cache_trace": False},
            }
            config["model"]["params"] = {
                "contextWindow": 131072,
                "maxTokens": 32768,
            }
            path = self._write(root, "run.yaml", config)
            plan = load_bench_run_plan(
                path,
                environ={
                    "MODEL_BASE_URL": "https://model.example/v1",
                    "MODEL_API_KEY": "secret-value",
                },
                temporary_root=root,
            )
            argv = list(plan.collect_argv)
            model_config = Path(argv[argv.index("--model-config") + 1])
            payload = json.loads(model_config.read_text(encoding="utf-8"))

            provider = payload["providers"]["example"]
            self.assertEqual(provider["baseUrl"], "https://model.example/v1")
            self.assertEqual(provider["apiKey"], "${MODEL_API_KEY}")
            self.assertEqual(provider["models"][0]["id"], "model")
            self.assertEqual(provider["models"][0]["maxTokens"], 32768)
            self.assertIn("--no-cache-trace", argv)
            self.assertNotIn("secret-value", model_config.read_text(encoding="utf-8"))

    def test_openclaw_provider_keeps_upstream_model_id_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._base()
            config["benchmark"] = {"id": "pinchbench", "root": "/bench/pinch"}
            config["harness"] = {"id": "openclaw"}
            config["model"]["id"] = "organization/model"
            config["model"]["provider"] = "roller"
            path = self._write(root, "run.yaml", config)
            plan = load_bench_run_plan(
                path,
                environ={
                    "MODEL_BASE_URL": "https://model.example/v1",
                    "MODEL_API_KEY": "secret-value",
                },
                temporary_root=root,
            )
            argv = list(plan.collect_argv)
            model_config = Path(argv[argv.index("--model-config") + 1])
            payload = json.loads(model_config.read_text(encoding="utf-8"))

            self.assertEqual(argv[argv.index("--model") + 1], "roller/organization/model")
            self.assertEqual(
                payload["providers"]["roller"]["models"][0]["id"],
                "organization/model",
            )
            self.assertEqual(
                plan.provenance["effective"]["model"]["provider"], "roller"
            )

    def test_openclaw_defaults_provider_for_plain_model_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._base()
            config["benchmark"] = {"id": "pinchbench", "root": "/bench/pinch"}
            config["harness"] = {"id": "openclaw"}
            config["model"]["id"] = "plain-model"
            path = self._write(root, "run.yaml", config)
            plan = load_bench_run_plan(
                path,
                environ={
                    "MODEL_BASE_URL": "https://model.example/v1",
                    "MODEL_API_KEY": "secret-value",
                },
                temporary_root=root,
            )
            argv = list(plan.collect_argv)
            model_config = Path(argv[argv.index("--model-config") + 1])
            payload = json.loads(model_config.read_text(encoding="utf-8"))

            self.assertEqual(argv[argv.index("--model") + 1], "roller/plain-model")
            self.assertEqual(
                payload["providers"]["roller"]["models"][0]["id"], "plain-model"
            )

    def test_non_openclaw_keeps_model_id_when_provider_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._base()
            config["benchmark"] = {"id": "skillsbench", "root": "/bench/skills"}
            config["harness"] = {"id": "opencode"}
            config["model"]["id"] = "organization/model"
            config["model"]["provider"] = "roller"
            path = self._write(root, "run.yaml", config)
            plan = load_bench_run_plan(
                path,
                environ={
                    "MODEL_BASE_URL": "https://model.example/v1",
                    "MODEL_API_KEY": "secret-value",
                },
                temporary_root=root,
            )
            argv = list(plan.collect_argv)

            self.assertEqual(argv[argv.index("--model") + 1], "organization/model")
            self.assertNotIn("roller/organization/model", argv)

    def test_openclaw_custom_model_config_can_clear_common_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_config = self._base()
            base_config["benchmark"] = {"id": "pinchbench", "root": "/bench"}
            base_config["harness"] = {"id": "openclaw"}
            base = self._write(root, "base.yaml", base_config)
            model_config = root / "models.json"
            model_config.write_text("{}", encoding="utf-8")
            override = self._write(
                root,
                "custom.yaml",
                {
                    "model": {"base_url": None, "api_key_env": None},
                    "harness": {"extra": {"model_config": str(model_config)}},
                },
            )

            plan = load_bench_run_plan(
                base,
                [str(override)],
                environ={
                    "MODEL_BASE_URL": "https://model.example/v1",
                    "MODEL_API_KEY": "secret-value",
                },
                temporary_root=root,
            )

        argv = list(plan.collect_argv)
        self.assertEqual(argv[argv.index("--model-config") + 1], str(model_config))
        self.assertNotIn("--api-base", argv)
        self.assertNotIn("--api-key-env", argv)

    def test_inline_extra_config_replaces_lists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._base()
            config["benchmark"] = {
                "id": "skillsbench",
                "root": "/bench/skills",
                "task_ids": ["first", "second"],
            }
            config["harness"] = {"id": "opencode"}
            path = self._write(root, "run.yaml", config)
            plan = load_bench_run_plan(
                path,
                [
                    json.dumps(
                        {
                            "benchmark": {
                                "variant": "no-skill",
                                "task_ids": ["only"],
                            },
                            "harness": {
                                "extra": {
                                    "max_steps": 100,
                                    "output_tokens": 32768,
                                    "vision": False,
                                }
                            },
                        }
                    )
                ],
                environ={
                    "MODEL_BASE_URL": "https://model.example/v1",
                    "MODEL_API_KEY": "secret-value",
                },
                temporary_root=root,
            )

        argv = list(plan.collect_argv)
        self.assertEqual(argv.count("--bench-task-id"), 1)
        self.assertEqual(argv[argv.index("--bench-task-id") + 1], "only")
        self.assertIn("--no-opencode-vision", argv)
        self.assertEqual(argv[argv.index("--opencode-max-steps") + 1], "100")

    def test_null_clears_harness_specific_settings_when_switching_harness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._base()
            config["benchmark"] = {"id": "wildclawbench", "root": "/bench"}
            config["harness"] = {
                "id": "openclaw",
                "image": "wildclawbench-openclaw:latest",
                "extra": {"max_tokens": 32768},
            }
            base = self._write(root, "base.yaml", config)
            switch = self._write(
                root,
                "codex.yaml",
                {
                    "harness": {
                        "id": "codex",
                        "image": "wildclawbench-codex:latest",
                        "extra": None,
                    }
                },
            )
            plan = load_bench_run_plan(
                base,
                [str(switch)],
                environ={
                    "MODEL_BASE_URL": "https://model.example/v1",
                    "MODEL_API_KEY": "secret-value",
                },
                temporary_root=root,
            )

        argv = list(plan.collect_argv)
        self.assertEqual(argv[argv.index("--harness") + 1], "codex")
        self.assertEqual(
            argv[argv.index("--docker-image") + 1],
            "wildclawbench-codex:latest",
        )
        self.assertNotIn("--openclaw-max-tokens", argv)
        self.assertEqual(
            plan.provenance["effective"]["model"]["api_protocol"],
            "openai-responses",
        )

    def test_thinking_alias_is_canonical_in_arguments_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._base()
            config["benchmark"] = {"id": "pinchbench", "root": "/bench"}
            config["model"]["thinking"] = "none"
            path = self._write(root, "run.yaml", config)
            plan = load_bench_run_plan(
                path,
                environ={
                    "MODEL_BASE_URL": "https://model.example/v1",
                    "MODEL_API_KEY": "secret-value",
                },
                temporary_root=root,
            )

        argv = list(plan.collect_argv)
        self.assertEqual(argv[argv.index("--thinking") + 1], "off")
        self.assertEqual(
            plan.provenance["effective"]["model"]["thinking"], "off"
        )

    def test_environment_references_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._base()
            config["benchmark"] = {"id": "pinchbench", "root": "${ROOT}/pinch"}
            path = self._write(root, "run.yaml", config)
            with self.assertRaisesRegex(ValueError, "complete value"):
                load_bench_run_plan(
                    path,
                    environ={
                        "ROOT": "/bench",
                        "MODEL_BASE_URL": "https://model.example/v1",
                        "MODEL_API_KEY": "secret-value",
                    },
                    temporary_root=root,
                )

            config["benchmark"]["root"] = "${MISSING_ROOT}"
            path = self._write(root, "run.yaml", config)
            with self.assertRaisesRegex(ValueError, "MISSING_ROOT"):
                load_bench_run_plan(
                    path,
                    environ={
                        "MODEL_BASE_URL": "https://model.example/v1",
                        "MODEL_API_KEY": "secret-value",
                    },
                    temporary_root=root,
                )

    def test_unknown_harness_extra_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._base()
            config["benchmark"] = {"id": "skillsbench", "root": "/bench"}
            config["harness"] = {"id": "opencode", "extra": {"typo": 1}}
            path = self._write(root, "run.yaml", config)
            with self.assertRaisesRegex(ValueError, "harness.extra has unknown"):
                load_bench_run_plan(
                    path,
                    environ={
                        "MODEL_BASE_URL": "https://model.example/v1",
                        "MODEL_API_KEY": "secret-value",
                    },
                    temporary_root=root,
                )

    def test_protocol_must_match_non_openclaw_harness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._base()
            config["benchmark"] = {"id": "wildclawbench", "root": "/bench"}
            config["harness"] = {"id": "codex"}
            config["model"]["api_protocol"] = "openai-completions"
            path = self._write(root, "run.yaml", config)
            with self.assertRaisesRegex(ValueError, "openai-responses"):
                load_bench_run_plan(
                    path,
                    environ={
                        "MODEL_BASE_URL": "https://model.example/v1",
                        "MODEL_API_KEY": "secret-value",
                    },
                    temporary_root=root,
                )

    def test_common_model_connection_maps_to_every_non_openclaw_harness(self) -> None:
        protocols = {
            "codex": "openai-responses",
            "claude-code": "anthropic-messages",
            "hermes": "openai-completions",
            "opencode": "openai-completions",
            "deepseek": "openai-completions",
            "pi": "openai-completions",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for harness, protocol in protocols.items():
                with self.subTest(harness=harness):
                    config = self._base()
                    config["benchmark"] = {
                        "id": "wildclawbench",
                        "root": "/bench",
                    }
                    config["harness"] = {"id": harness}
                    config["model"]["api_protocol"] = protocol
                    path = self._write(root, f"{harness}.yaml", config)
                    plan = load_bench_run_plan(
                        path,
                        environ={
                            "MODEL_BASE_URL": "https://model.example/v1",
                            "MODEL_API_KEY": "secret-value",
                        },
                        temporary_root=root,
                    )

                    argv = list(plan.collect_argv)
                    self.assertEqual(argv[argv.index("--harness") + 1], harness)
                    self.assertEqual(
                        argv[argv.index("--api-base") + 1],
                        "https://model.example/v1",
                    )
                    self.assertEqual(
                        argv[argv.index("--api-key-env") + 1], "MODEL_API_KEY"
                    )
                    self.assertEqual(
                        plan.provenance["effective"]["model"]["api_protocol"],
                        protocol,
                    )

    def test_benchmark_environment_is_scoped_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._base()
            config["benchmark"] = {
                "id": "pinchbench",
                "root": "/bench",
                "environment": {"PINCHBENCH_JUDGE_API_KEY": "${JUDGE_KEY}"},
            }
            path = self._write(root, "run.yaml", config)
            plan = load_bench_run_plan(
                path,
                environ={
                    "MODEL_BASE_URL": "https://model.example/v1",
                    "MODEL_API_KEY": "model-secret",
                    "JUDGE_KEY": "judge-secret",
                },
                temporary_root=root,
            )

        self.assertEqual(
            plan.environment["PINCHBENCH_JUDGE_API_KEY"], "judge-secret"
        )
        self.assertNotIn("judge-secret", json.dumps(plan.provenance))
        old = os.environ.get("PINCHBENCH_JUDGE_API_KEY")
        with temporary_environment(plan.environment):
            self.assertEqual(os.environ["PINCHBENCH_JUDGE_API_KEY"], "judge-secret")
        self.assertEqual(os.environ.get("PINCHBENCH_JUDGE_API_KEY"), old)

    def test_endpoint_rejects_credentials_query_and_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, endpoint in enumerate(
                (
                    "https://user:password@model.example:8443/v1",
                    "https://model.example/v1?token=secret",
                    "https://model.example/v1#fragment",
                )
            ):
                with self.subTest(endpoint=endpoint):
                    config = self._base()
                    config["benchmark"] = {"id": "pinchbench", "root": "/bench"}
                    config["model"]["base_url"] = endpoint
                    path = self._write(root, f"run-{index}.yaml", config)
                    with self.assertRaisesRegex(
                        ValueError, "credentials|query or fragment"
                    ):
                        load_bench_run_plan(
                            path,
                            environ={"MODEL_API_KEY": "model-secret"},
                            temporary_root=root,
                        )

    def test_effective_endpoint_preserves_ipv6_literal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._base()
            config["benchmark"] = {"id": "pinchbench", "root": "/bench"}
            config["model"]["base_url"] = "http://[::1]:8080/v1"
            path = self._write(root, "run.yaml", config)
            plan = load_bench_run_plan(
                path,
                environ={"MODEL_API_KEY": "model-secret"},
                temporary_root=root,
            )

        self.assertEqual(
            plan.provenance["effective"]["model"]["base_url"],
            "http://[::1]:8080/v1",
        )


class BenchRunCliTests(unittest.TestCase):
    def _pinch_root(self, root: Path) -> Path:
        bench = root / "pinch"
        tasks = bench / "tasks"
        tasks.mkdir(parents=True)
        (bench / "assets").mkdir()
        (tasks / "manifest.yaml").write_text(
            "run_first: [task_sanity]\ncategories:\n  setup: [task_sanity]\n",
            encoding="utf-8",
        )
        (tasks / "task_sanity.md").write_text(
            """---
id: task_sanity
name: Sanity
category: setup
grading_type: automated
timeout_seconds: 45
---

## Prompt

Say hello.

## Automated Checks

```python
def grade(transcript, workspace_path):
    return {"ok": 1.0}
```
""",
            encoding="utf-8",
        )
        return bench

    def test_bench_run_dry_run_reaches_existing_collect_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bench = self._pinch_root(root)
            config = {
                "schema_version": BENCH_RUN_SCHEMA,
                "benchmark": {
                    "id": "pinchbench",
                    "root": str(bench),
                    "variant": "automated-only",
                    "task_ids": ["task_sanity"],
                },
                "model": {
                    "id": "example/model",
                    "base_url": "${MODEL_BASE_URL}",
                    "api_key_env": "MODEL_API_KEY",
                    "thinking": "off",
                },
                "harness": {"id": "openclaw"},
                "execution": {"parallel": 2, "samples": 1},
            }
            path = root / "run.yaml"
            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            args = _build_parser().parse_args(
                ["bench", "run", "--config", str(path), "--dry-run"]
            )
            output = io.StringIO()
            with patch.dict(
                os.environ,
                {
                    "MODEL_BASE_URL": "https://model.example/v1",
                    "MODEL_API_KEY": "secret-value",
                },
                clear=False,
            ), contextlib.redirect_stdout(output):
                result = _bench(args)

        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["bench"]["name"], "pinchbench")
        self.assertEqual(payload["tasks"], 1)
        self.assertEqual(payload["trajectories"], 1)
        self.assertEqual(payload["parallel"], 2)
        self.assertEqual(payload["plan"][0]["timeout_seconds"], 45)
        self.assertEqual(
            payload["bench_run_config"]["schema_version"], BENCH_RUN_SCHEMA
        )
        self.assertNotIn("secret-value", output.getvalue())


if __name__ == "__main__":
    unittest.main()
