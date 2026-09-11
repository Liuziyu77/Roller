"""Opt-in real Hermes/Docker test with a deterministic local model endpoint.

ROLLER_HERMES_DOCKER_TEST=1 PYTHONPATH=src python -m unittest discover \
    -s tests -p test_hermes_docker.py -v
"""
from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import threading
import unittest
from unittest.mock import patch

from roller.cli import _build_parser, _collect
from roller.harnesses.hermes import DEFAULT_HERMES_IMAGE, HermesConfig, HermesHarness
from roller.models import RunRequest, Task


@unittest.skipUnless(os.environ.get("ROLLER_HERMES_DOCKER_TEST") == "1", "set ROLLER_HERMES_DOCKER_TEST=1 to run real Docker")
class HermesDockerTests(unittest.TestCase):
    def test_real_tools_multiturn_full_trace_and_resume(self):
        requests = []
        script = (
            "from pathlib import Path; import os; "
            "parts=[Path('note.txt').read_text().strip(),Path('MEMORY.md').read_text().strip(),"
            "Path('scratch/setup.txt').read_text().strip(),os.environ['ROLLER_EXAMPLE_TOKEN']]; "
            "\ntry:\n Path('note.txt').write_text('MUTATED')\n"
            "except OSError:\n parts.append('READ_ONLY')\n"
            "else:\n parts.append('WRITABLE')\n"
            "Path('outputs/answer.txt').write_text('|'.join(parts)); "
            "print('WROTE_TOKEN')"
        )

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, payload):
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.reply({"object": "list", "data": [{"id": "roller-test", "object": "model", "context_length": 128000}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                message = {"role": "assistant", "content": "DONE", "reasoning_content": "The requested work is complete."}
                if len(requests) == 1:
                    message.update(content=None, tool_calls=[{"id": "call-local-1", "type": "function", "function": {"name": "terminal", "arguments": json.dumps({"command": "python -c " + shlex.quote(script)})}}])
                self.reply({"id": f"chatcmpl-{len(requests)}", "object": "chat.completion", "created": 1, "model": "roller-test", "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(prefix="roller-hermes-docker-") as tmp:
            root = Path(os.environ.get("ROLLER_HERMES_DOCKER_OUTPUT", tmp))
            root.mkdir(parents=True, exist_ok=True)
            (root / "note.txt").write_text("INPUT")
            (root / "memory.md").write_text("CONTEXT")
            tasks = root / "tasks.json"
            tasks.write_text(json.dumps([{
                "id": "hermes-docker", "harness": "hermes", "model": "roller-test",
                "messages": ["Read task files and write outputs/answer.txt; reply DONE.", "Remember the previous task and reply DONE again."],
                "files": {"inputs": ["note.txt"], "context": [{"role": "memory", "source": "memory.md"}]},
                "runtime": {"setup": "mkdir -p scratch && printf SETUP > scratch/setup.txt", "env": ["ROLLER_EXAMPLE_TOKEN"]},
            }]))
            command = ["collect", "--tasks", str(tasks), "--hermes-image", os.environ.get("ROLLER_HERMES_IMAGE", DEFAULT_HERMES_IMAGE), "--hermes-api-base", f"http://127.0.0.1:{server.server_port}/v1", "--hermes-api-key-env", "ROLLER_LOCAL_KEY", "--wire-trace", "full", "--timeout", "240", "--output", str(root / "runs"), "--run-name", "local"]
            environment = {"ROLLER_LOCAL_KEY": "test-local-secret", "ROLLER_EXAMPLE_TOKEN": "ENV", "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"}
            with patch.dict(os.environ, environment), contextlib.redirect_stdout(io.StringIO()):
                code = _collect(_build_parser().parse_args(command))
                infos = list((root / "runs/local/samples").glob("*/*/info.json"))
                self.assertEqual(len(infos), 1)
                info = json.loads(infos[0].read_text())
                self.assertEqual(code, 0, info)
                sample = infos[0].parent
                self.assertEqual((sample / "results/answer.txt").read_text(), "INPUT|CONTEXT|SETUP|ENV|READ_ONLY")
                self.assertEqual((root / "note.txt").read_text(), "INPUT")
                raw = (sample / "trace.jsonl").read_text()
                sft = json.loads((sample / "trace_SFT.json").read_text())
                self.assertEqual(sum(m["role"] == "user" for m in sft["messages"]), 2)
                self.assertEqual(sum(m["role"] == "tool" for m in sft["messages"]), 1)
                self.assertIn("terminal", {t["function"]["name"] for t in sft["tools"]})
                self.assertIn("WROTE_TOKEN", raw)
                self.assertTrue((sample / "wire_trace.jsonl").is_file())
                self.assertFalse((sample / "cache_trace.jsonl").exists())
                self.assertNotIn("test-local-secret", raw)
                before = len(requests)
                self.assertEqual(_collect(_build_parser().parse_args(command + ["--resume"])), 0)
                self.assertEqual(len(requests), before)
                self.assertEqual((sample / "trace.jsonl").read_text(), raw)
                with self.assertRaisesRegex(Exception, "lock|configuration|mismatch"):
                    _collect(_build_parser().parse_args(command + ["--resume", "--hermes-max-iterations", "3"]))
                # A second native session verifies export-only reasoning removal
                # and schema capture without the HTTP proxy.
                harness = HermesHarness(HermesConfig(
                    api_base=f"http://127.0.0.1:{server.server_port}/v1", api_key_env="ROLLER_LOCAL_KEY",
                    docker_image=os.environ.get("ROLLER_HERMES_IMAGE", DEFAULT_HERMES_IMAGE),
                    wire_trace_mode="off", keep_thinking=False, context_length=128000,
                ), 0)
                destination = root / "no-thinking"
                result = harness.run(RunRequest(0, Task("reasoning", ("Reply DONE",), 0, harness="hermes"), "roller-test", 120), destination, 1)
                self.assertEqual(result.status, "ok", result.error)
                trace = json.loads((destination / result.trajectory_path).read_text())
                self.assertTrue(trace["tools"])
                self.assertTrue(all(s["assistant_content"]["reasoning_content"] == "" for s in trace["steps"]))
                self.assertIn("The requested work is complete.", (destination / result.trace_path).read_text())
                self.assertFalse((destination / "wire_trace.jsonl").exists())
            self.assertGreaterEqual(len(requests), 3)
            self.assertTrue(any(m.get("role") == "assistant" and m.get("content") == "DONE" for m in requests[2]["messages"]))

    def test_timeout_recovers_partial_outputs_and_removes_container(self):
        with tempfile.TemporaryDirectory(prefix="roller-hermes-timeout-") as tmp, patch.dict(os.environ, {"ROLLER_LOCAL_KEY": "test-local-secret"}):
            harness = HermesHarness(HermesConfig(
                api_base="http://127.0.0.1:1/v1", api_key_env="ROLLER_LOCAL_KEY", wire_trace_mode="off",
                docker_image=os.environ.get("ROLLER_HERMES_IMAGE", DEFAULT_HERMES_IMAGE),
            ), 0)
            task = Task("timeout", ("wait",), 0, harness="hermes", setup_commands=("printf PARTIAL > outputs/partial.txt",))
            session = harness.create_session(RunRequest(0, task, "roller-test", 120), Path(tmp), 1)
            try:
                session.start()
                with self.assertRaises(TimeoutError):
                    session.send("wait", 0.001)
            finally:
                result = session.finish("timeout", "test deadline")
            self.assertEqual(result.status, "timeout")
            self.assertEqual((Path(tmp) / "results/partial.txt").read_text(), "PARTIAL")
            self.assertNotEqual(harness.runtime.run(["inspect", session.container], timeout=10).returncode, 0)


if __name__ == "__main__":
    unittest.main()
