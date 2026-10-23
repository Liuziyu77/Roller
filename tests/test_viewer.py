from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import urlopen

from roller.cli import _build_parser
from roller.viewer import RollerRun, make_server
from roller.viewer import app as viewer_app


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class ViewerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "run"
        self.root.mkdir()
        write_json(
            self.root / "info.json",
            {
                "schema_version": "roller.run/v1",
                "roller_version": "0.2.0",
                "state": "running",
                "updated_at": "2026-08-21T10:00:03+00:00",
                "configuration": {
                    "harness": "openclaw",
                    "trajectory_count": 2,
                    "openclaw": {
                        "thinking": "high",
                        "keep_thinking": True,
                    },
                },
                "summary": {
                    "total": 3,
                    "processed": 2,
                    "pending": 1,
                    "usage": {"total_tokens": 17, "request_count": 2},
                },
                "samples": [
                    {
                        "task_id": "shared-task",
                        "model": "provider/model-a",
                        "status": "ok",
                        "path": "samples/model-a/00000000_shared-task",
                    },
                    {
                        "task_id": "shared-task",
                        "model": "provider/model-b",
                        "status": "error",
                        "path": "samples/model-b/00000000_shared-task",
                    },
                    {
                        "task_id": "escape",
                        "model": "bad/model",
                        "status": "ok",
                        "path": "../outside",
                    },
                ],
            },
        )
        self.sample_a = self.root / "samples/model-a/00000000_shared-task"
        self.sample_b = self.root / "samples/model-b/00000000_shared-task"
        write_json(
            self.sample_a / "info.json",
            {
                "schema_version": "roller.sample/v1",
                "request_index": 0,
                "source_index": 0,
                "task_id": "shared-task",
                "model": "provider/model-a",
                "harness": "openclaw",
                "status": "ok",
                "error": None,
                "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
                "timing": {"elapsed_seconds": 2.5},
                "attempts": [{"attempt": 1, "status": "ok"}],
                "trajectory": {"actf": "trace_ACTF.json"},
            },
        )
        write_json(
            self.sample_a / "trace_ACTF.json",
            {
                "schema_version": "ACTF_v1.0",
                "system_prompt": "System prompt",
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "read", "parameters": {"type": "object"}},
                    }
                ],
                "steps": [
                    {
                        "step_id": 1,
                        "user_content": "Read the file",
                        "assistant_content": {
                            "reasoning_content": "r" * 2_000,
                            "content": "done",
                            "tool_calls": [
                                {
                                    "type": "toolCall",
                                    "id": "call-1",
                                    "name": "read",
                                    "arguments": {"path": "/tmp/input.txt"},
                                }
                            ],
                        },
                        "observation": [
                            {
                                "role": "toolResult",
                                "toolCallId": "call-1",
                                "toolName": "read",
                                "isError": False,
                                "content": [{"type": "text", "text": "file content"}],
                            }
                        ],
                        "metric": {
                            "prompt_tokens_len": 5,
                            "completion_tokens_len": 3,
                            "stop_reason": "toolUse",
                        },
                        "started_at": "2026-08-21T10:00:00+00:00",
                        "finished_at": "2026-08-21T10:00:02+00:00",
                    }
                ],
                "started_at": "2026-08-21T10:00:00+00:00",
                "finished_at": "2026-08-21T10:00:02+00:00",
            },
        )
        write_json(
            self.sample_b / "info.json",
            {
                "schema_version": "roller.sample/v1",
                "request_index": 1,
                "source_index": 0,
                "task_id": "shared-task",
                "model": "provider/model-b",
                "harness": "openclaw",
                "status": "error",
                "error": "model failed",
                "usage": {"total_tokens": 9},
                "trajectory": {"actf": "../../../../outside.json"},
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_indexes_tasks_groups_models_and_ignores_escaping_paths(self) -> None:
        source = RollerRun(self.root)

        summary = source.run_summary()
        self.assertEqual(summary["task_count"], 1)
        self.assertEqual(summary["rollout_count"], 2)
        self.assertEqual(summary["status_counts"], {"ok": 1, "error": 1})
        self.assertEqual(summary["configuration"]["thinking"], "high")
        self.assertEqual(summary["state"], "running")
        self.assertEqual(
            summary["progress"], {"total": 3, "processed": 2, "pending": 1}
        )

        tasks = source.list_tasks(page_size=10)
        self.assertEqual(tasks["total"], 1)
        self.assertEqual(len(tasks["items"][0]["rollouts"]), 2)
        errors = source.list_tasks(status="error", query="model-b")
        self.assertEqual(errors["total"], 1)
        self.assertEqual(errors["items"][0]["rollouts"][0]["status"], "error")

    def test_run_checkpoint_status_overlays_finalized_sample_status(self) -> None:
        info_path = self.root / "info.json"
        run_info = json.loads(info_path.read_text(encoding="utf-8"))
        run_info["samples"][0]["status"] = "skipped"
        write_json(info_path, run_info)

        source = RollerRun(self.root)
        rollout = source.list_tasks(model="provider/model-a")["items"][0]["rollouts"][0]

        self.assertEqual(rollout["status"], "skipped")
        self.assertEqual(source.run_summary()["status_counts"]["skipped"], 1)

    def test_projects_codex_reasoning_configuration(self) -> None:
        info_path = self.root / "info.json"
        run_info = json.loads(info_path.read_text(encoding="utf-8"))
        run_info["configuration"]["harness"] = "codex"
        run_info["configuration"]["codex"] = {
            "reasoning_effort": "high",
            "reasoning_summary": "detailed",
            "keep_thinking": True,
            "supports_websockets": False,
        }
        write_json(info_path, run_info)

        configuration = RollerRun(self.root).run_summary()["configuration"]

        self.assertEqual(configuration["harness"], "codex")
        self.assertEqual(configuration["thinking"], "high")
        self.assertEqual(configuration["reasoning_summary"], "detailed")
        self.assertTrue(configuration["keep_thinking"])

    def test_projects_actf_summaries_and_truncates_large_step_fields(self) -> None:
        source = RollerRun(self.root)
        rollout = source.list_tasks(model="provider/model-a")["items"][0]["rollouts"][0]

        detail = source.rollout_detail(rollout["id"])
        trajectory = detail["trajectory"]
        assert trajectory is not None
        self.assertEqual(trajectory["step_count"], 1)
        self.assertEqual(trajectory["steps"][0]["reasoning_chars"], 2_000)
        self.assertEqual(trajectory["steps"][0]["tool_call_count"], 1)

        step = source.step_detail(rollout["id"], 0, text_limit=1_000)
        self.assertTrue(step["assistant"]["reasoning"]["truncated"])
        self.assertEqual(len(step["assistant"]["reasoning"]["text"]), 1_000)
        self.assertEqual(step["assistant"]["tool_calls"][0]["name"], "read")
        self.assertEqual(step["observations"][0]["tool_call_id"], "call-1")

        failed = source.list_tasks(model="provider/model-b")["items"][0]["rollouts"][0]
        self.assertFalse(failed["has_actf"])
        self.assertIsNone(source.rollout_detail(failed["id"])["trajectory"])

    def test_serves_static_ui_and_read_only_api(self) -> None:
        server = make_server(self.root, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        address = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urlopen(f"{address}/", timeout=5) as response:
                html = response.read().decode("utf-8")
                self.assertIn("Roller Trajectory Viewer", html)
                self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
            with urlopen(f"{address}/style.css", timeout=5) as response:
                css = response.read().decode("utf-8")
                self.assertIn("[hidden] { display: none !important; }", css)
            with urlopen(f"{address}/api/v1/run", timeout=5) as response:
                payload = json.loads(response.read())
                self.assertEqual(payload["rollout_count"], 2)
            with urlopen(f"{address}/api/v1/tasks?page_size=1", timeout=5) as response:
                payload = json.loads(response.read())
                self.assertEqual(payload["page_size"], 1)
                self.assertEqual(payload["total"], 1)
            with self.assertRaises(HTTPError) as raised:
                urlopen(f"{address}/api/v1/rollouts/not-found", timeout=5)
            self.assertEqual(raised.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_cli_exposes_view_command_with_loopback_defaults(self) -> None:
        args = _build_parser().parse_args(["view", str(self.root)])
        self.assertEqual(args.command, "view")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertIsNone(args.port)

    def test_busy_default_port_falls_back_to_a_free_port(self) -> None:
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen()
            busy = blocker.getsockname()[1]
            with mock.patch.object(viewer_app, "DEFAULT_PORT", busy):
                server = make_server(self.root)
            try:
                self.assertNotEqual(server.server_address[1], busy)
                self.assertGreater(server.server_address[1], 0)
            finally:
                server.server_close()

    def test_busy_explicit_port_is_an_error(self) -> None:
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen()
            busy = blocker.getsockname()[1]
            with self.assertRaisesRegex(RuntimeError, "another --port"):
                make_server(self.root, port=busy)


if __name__ == "__main__":
    unittest.main()
