"""Opt-in real Pi/Docker test with a deterministic local model endpoint.

ROLLER_PI_DOCKER_TEST=1 PYTHONPATH=src python -m unittest discover \
    -s tests -p test_pi_docker.py -v

Also set ROLLER_PI_MIXED_DOCKER_TEST=1 to exercise Pi/DeepSeek pairings;
that optional test additionally requires the configured DeepSeek image.
"""
from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
import threading
import unittest
from unittest.mock import patch

from roller.cli import _build_parser, _collect
from roller.harnesses.pi import DEFAULT_PI_IMAGE, PiConfig, PiHarness
from roller.models import RunRequest, Task


@unittest.skipUnless(os.environ.get("ROLLER_PI_DOCKER_TEST") == "1", "set ROLLER_PI_DOCKER_TEST=1 to run real Docker")
class PiDockerTests(unittest.TestCase):
    def test_all_seven_native_tools_and_exact_model_id(self):
        requests = []
        operations = [
            ("read", {"path": "note.txt"}),
            ("write", {"path": "outputs/native.txt", "content": "BEFORE\n"}),
            ("edit", {"path": "outputs/native.txt", "oldText": "BEFORE", "newText": "AFTER"}),
            ("grep", {"pattern": "AFTER", "path": "outputs"}),
            ("find", {"pattern": "*.txt", "path": "outputs"}),
            ("ls", {"path": "outputs"}),
            ("bash", {"command": "cat outputs/native.txt > outputs/copy.txt"}),
        ]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                index = len(requests) - 1
                delta = {"role": "assistant", "content": "DONE"}
                reason = "stop"
                if index < len(operations):
                    name, arguments = operations[index]
                    delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": f"native-{index}", "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)}}]}
                    reason = "tool_calls"
                chunk = {"id": "native", "object": "chat.completion.chunk", "created": 1, "model": "gateway/model"}
                chunks = [{**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}]
                data = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(prefix="roller-pi-tools-") as tmp, patch.dict(os.environ, {"ROLLER_LOCAL_KEY": "test-local-secret"}):
            root = Path(tmp)
            harness = PiHarness(PiConfig(
                api_base=f"http://127.0.0.1:{server.server_port}/v1", api_key_env="ROLLER_LOCAL_KEY",
                reasoning_effort="high", wire_trace_mode="off",
                docker_image=os.environ.get("ROLLER_PI_IMAGE", DEFAULT_PI_IMAGE),
            ), 0)
            task = Task("native-tools", ("Read note.txt\u2028中文\u2029 and exercise the requested tools.",), 0, harness="pi",
                setup_commands=("printf INPUT > note.txt",))
            result = harness.run(RunRequest(0, task, "gateway/model", 120), root, 1)
            self.assertEqual(result.status, "ok", result.error)
            self.assertEqual((root / "results/native.txt").read_text(), "AFTER\n")
            self.assertEqual((root / "results/copy.txt").read_text(), "AFTER\n")
            self.assertEqual(len(requests), 8)
            for request in requests:
                self.assertEqual(request["model"], "gateway/model")
                self.assertEqual(request["reasoning_effort"], "high")
                self.assertEqual({t["function"]["name"] for t in request["tools"]}, {name for name, _ in operations})
            from roller.pi_actf import read_pi_events
            from roller.sft import actf_to_sft
            events = read_pi_events(root / result.trace_path)
            results = [e["event"]["message"] for e in events if e.get("event", {}).get("type") == "message_end"
                and e["event"]["message"]["role"] == "toolResult"]
            self.assertEqual(len(results), 7)
            self.assertTrue(all(not message["isError"] for message in results), results)
            sft = actf_to_sft(json.loads((root / result.trajectory_path).read_text()))
            self.assertIn("\u2028中文\u2029", sft["messages"][1]["content"])
            self.assertEqual(sft["tools"], requests[0]["tools"])
            self.assertEqual([e["body"] for e in events if e["type"] == "request"], requests)
            def normalized(messages):
                result = json.loads(json.dumps(messages))
                for message in result:
                    if isinstance(message.get("content"), list):
                        self.assertTrue(all(block.get("type") == "text" for block in message["content"]))
                        message["content"] = "\n".join(block["text"] for block in message["content"])
                    for key in ("content", "reasoning_content", "tool_calls"):
                        if message.get(key) in (None, "", []):
                            message.pop(key, None)
                    for call in message.get("tool_calls", []):
                        call["function"]["arguments"] = json.loads(call["function"]["arguments"])
                return result
            # Every API call must see the same conversation prefix that the
            # exported training sample represents, including native tool output.
            positions = [i for i, message in enumerate(sft["messages"]) if message["role"] == "assistant"]
            for request, position in zip(requests, positions):
                self.assertEqual(normalized(request["messages"]), normalized(sft["messages"][:position]))

    def test_four_real_simulator_conversations_and_resume(self):
        self._simulator_conversations()

    @unittest.skipUnless(os.environ.get("ROLLER_PI_MIXED_DOCKER_TEST") == "1",
                         "set ROLLER_PI_MIXED_DOCKER_TEST=1 and provide a DeepSeek image")
    def test_mixed_deepseek_participants_and_resume(self):
        self._simulator_conversations(mixed=True)

    def _simulator_conversations(self, mixed=False):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                messages = body["messages"]
                prompt = next(m["content"] for m in reversed(messages) if m["role"] == "user")
                if isinstance(prompt, list):
                    prompt = "\n".join(block["text"] for block in prompt)
                delta = {"role": "assistant"}
                finish = "stop"
                if prompt.startswith("You are the User Simulator"):
                    state = json.loads(prompt.split("Conversation manager state:\n", 1)[1])
                    spec = json.loads(state["instruction"])
                    if state["rounds_completed"] >= spec["rounds"]:
                        decision = {"action": "finish", "reason": "done"}
                    else:
                        verb = "Write" if not state["rounds_completed"] else "Read"
                        decision = {"action": "message", "content": f"{verb} {spec['token']} to {spec['output']}/pair.txt and reply DONE."}
                    delta["content"] = json.dumps(decision)
                elif messages[-1]["role"] == "tool":
                    delta["content"] = "DONE"
                else:
                    match = re.fullmatch(r"(Write|Read) (LOCAL_PAIR_\d) to ([a-z/.]+) and reply DONE\.", prompt)
                    if match is None:
                        raise AssertionError(prompt)
                    verb, token, path = match.groups()
                    command = f"mkdir -p {shlex.quote(str(Path(path).parent))}; printf %s {shlex.quote(token)} > {shlex.quote(path)}" if verb == "Write" else "cat " + shlex.quote(path)
                    delta["tool_calls"] = [{"index": 0, "id": f"pair-call-{len(requests)}", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": command})}}]
                    finish = "tool_calls"
                chunk = {"id": f"local-{len(requests)}", "object": "chat.completion.chunk", "created": 1, "model": "roller-test"}
                chunks = [{**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                          {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}]
                data = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(prefix="roller-pi-simulator-") as tmp:
            root = Path(os.environ.get("ROLLER_PI_SIMULATOR_OUTPUT", tmp)).resolve()
            root.mkdir(parents=True, exist_ok=True)
            rows = []
            for index in range(4):
                rounds = 2 if index in {1, 3} else 1
                output = "artifacts" if index == 2 else "outputs"
                agent = "deepseek" if mixed and index % 2 else "pi"
                simulator = "deepseek" if mixed and not index % 2 else "pi"
                rows.append({"id": f"pair-{index}", "harness": agent, "model": "roller-test",
                    "files": {"output_dir": output}, "user_simulator": {"harness": simulator,
                    "instruction": json.dumps({"token": f"LOCAL_PAIR_{index}", "output": output, "rounds": rounds}),
                    "max_turns": 1 if index == 3 else 3}})
            tasks = root / "tasks.json"
            tasks.write_text(json.dumps(rows))
            command = ["collect", "--tasks", str(tasks), "--pi-image", os.environ.get("ROLLER_PI_IMAGE", DEFAULT_PI_IMAGE),
                "--pi-api-base", f"http://127.0.0.1:{server.server_port}/v1", "--pi-api-key-env", "ROLLER_LOCAL_KEY",
                "--thinking", "off", "--wire-trace", "full", "--parallel", "2", "--timeout", "180",
                "--output", str(root / "runs"), "--run-name", "local"]
            if mixed:
                command += ["--deepseek-api-base", f"http://127.0.0.1:{server.server_port}/v1",
                    "--deepseek-api-key-env", "ROLLER_LOCAL_KEY"]
            with patch.dict(os.environ, {"ROLLER_LOCAL_KEY": "test-local-secret",
                    "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"}), contextlib.redirect_stdout(io.StringIO()):
                code = _collect(_build_parser().parse_args(command))
                infos = sorted((root / "runs/local/samples").glob("*/*/info.json"))
                self.assertEqual(len(infos), 4)
                self.assertEqual(code, 0, [json.loads(p.read_text()).get("error") for p in infos])
                traces = {}
                for index, info in enumerate(infos):
                    sample = info.parent
                    self.assertEqual((sample / "results/pair.txt").read_text(), f"LOCAL_PAIR_{index}")
                    conversation = json.loads((sample / "conversation.json").read_text())
                    self.assertEqual(len(conversation["rounds"]), 2 if index == 1 else 1)
                    self.assertEqual(conversation["termination"]["reason"], "max_turns" if index == 3 else "simulator_finished")
                    for participant in (sample, sample / "user_simulator"):
                        path = participant / "trace.jsonl"
                        traces[path] = path.read_bytes()
                        events = [json.loads(line) for line in path.read_text().splitlines()]
                        native = [e["body"] for e in events if e["type"] == "request"]
                        wire_events = [json.loads(line) for line in (participant / "wire_trace.jsonl").read_text().splitlines()]
                        wire = [event["body"] for event in wire_events if "body" in event]
                        self.assertEqual(native, wire)
                        self.assertTrue((participant / "trace_SFT.json").is_file())
                        self.assertFalse((participant / "cache_trace.jsonl").exists())
                before = len(requests)
                self.assertEqual(_collect(_build_parser().parse_args(command + ["--resume"])), 0)
                self.assertEqual(len(requests), before)
                self.assertTrue(all(path.read_bytes() == value for path, value in traces.items()))

    def test_step_limit_truncation_and_empty_reply_fail(self):
        requests = []
        mode = "steps"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                delta = {"role": "assistant", "content": "" if mode == "empty" else "partial"}
                if mode == "steps":
                    delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "limit-call",
                        "type": "function", "function": {"name": "bash", "arguments": json.dumps({
                            "command": "printf TOOL_RAN > outputs/partial.txt"})}}]}
                reason = "tool_calls" if mode == "steps" else "length" if mode == "length" else "stop"
                chunk = {"id": "local", "object": "chat.completion.chunk", "created": 1, "model": "roller-test"}
                chunks = [{**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                          {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}]
                data = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(prefix="roller-pi-limits-") as tmp, patch.dict(os.environ, {"ROLLER_LOCAL_KEY": "test-local-secret"}):
            for mode in ("steps", "length", "empty"):
                with self.subTest(mode=mode):
                    requests.clear()
                    harness = PiHarness(PiConfig(
                        api_base=f"http://127.0.0.1:{server.server_port}/v1", api_key_env="ROLLER_LOCAL_KEY",
                        max_steps=1, reasoning_effort="off",
                        docker_image=os.environ.get("ROLLER_PI_IMAGE", DEFAULT_PI_IMAGE),
                    ), 0)
                    root = Path(tmp) / mode
                    result = harness.run(RunRequest(0, Task(mode, ("Write partial and reply DONE",), 0,
                        harness="pi"), "roller-test", 90), root, 1)
                    self.assertEqual(result.status, "error", result.error)
                    self.assertIsNone(result.trajectory_path)
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0]["reasoning_effort"], "none")
                    self.assertEqual(requests[0]["max_tokens"], 8192)
                    if mode == "steps":
                        self.assertEqual((root / "results/partial.txt").read_text(), "TOOL_RAN")
                        self.assertIn("max steps exceeded", (root / result.trace_path).read_text())

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
                    message.update(content=None, tool_calls=[{"id": "call-local-1", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "printf KEPT > outputs/native-state.txt; python3 -c " + shlex.quote(script)})}}])
                elif len(requests) == 3:
                    message.update(content=None, tool_calls=[{"id": "call-local-2", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "cat outputs/native-state.txt > outputs/persistent.txt"})}}])
                payload = {"id": f"chatcmpl-{len(requests)}", "object": "chat.completion.chunk", "created": 1, "model": "roller-test"}
                chunks = []
                delta = {"role": "assistant", "reasoning_content": message["reasoning_content"]}
                chunks.append({**payload, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
                if message.get("tool_calls"):
                    delta = {"tool_calls": [{"index": i, **call} for i, call in enumerate(message["tool_calls"])]}
                else:
                    delta = {"content": message["content"]}
                chunks.append({**payload, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
                chunks.append({**payload, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}]})
                chunks.append({**payload, "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}})
                data = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(prefix="roller-pi-docker-") as tmp:
            root = Path(os.environ.get("ROLLER_PI_DOCKER_OUTPUT", tmp))
            root.mkdir(parents=True, exist_ok=True)
            (root / "note.txt").write_text("INPUT")
            (root / "memory.md").write_text("CONTEXT")
            tasks = root / "tasks.json"
            tasks.write_text(json.dumps([{
                "id": "pi-docker", "harness": "pi", "model": "roller-test",
                "messages": ["Read task files and write outputs/answer.txt; reply DONE.", "Remember the previous task and reply DONE again."],
                "files": {"inputs": ["note.txt"], "context": [{"role": "memory", "source": "memory.md"}]},
                "runtime": {"setup": "mkdir -p scratch && printf SETUP > scratch/setup.txt", "env": ["ROLLER_EXAMPLE_TOKEN"]},
                "bench": {"schema_version": "roller.bench/v1", "name": "pi-local", "category": "files",
                    "grade": {"needs_transcript": False, "timeout_seconds": 10,
                        "code": "from pathlib import Path\ndef grade(workspace_path):\n    root = Path(workspace_path)\n    return {'overall_score': float((root/'outputs/answer.txt').read_text() == 'INPUT|CONTEXT|SETUP|ENV|READ_ONLY' and (root/'outputs/persistent.txt').read_text() == 'KEPT')}\n"}},
            }]))
            command = ["collect", "--tasks", str(tasks), "--pi-image", os.environ.get("ROLLER_PI_IMAGE", DEFAULT_PI_IMAGE), "--pi-api-base", f"http://127.0.0.1:{server.server_port}/v1", "--pi-api-key-env", "ROLLER_LOCAL_KEY", "--wire-trace", "full", "--timeout", "240", "--output", str(root / "runs"), "--run-name", "local"]
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
                self.assertEqual((sample / "results/persistent.txt").read_text(), "KEPT")
                self.assertEqual(json.loads((sample / "score.json").read_text())["scores"]["overall_score"], 1.0)
                raw = (sample / "trace.jsonl").read_text()
                sft = json.loads((sample / "trace_SFT.json").read_text())
                self.assertEqual(sum(m["role"] == "user" for m in sft["messages"]), 2)
                self.assertEqual(sum(m["role"] == "tool" for m in sft["messages"]), 2)
                self.assertIn("bash", {t["function"]["name"] for t in sft["tools"]})
                self.assertIn("WROTE_TOKEN", raw)
                self.assertTrue((sample / "wire_trace.jsonl").is_file())
                native_requests = [json.loads(line)["body"] for line in raw.splitlines()
                                   if json.loads(line).get("type") == "request"]
                wire_events = [json.loads(line) for line in
                               (sample / "wire_trace.jsonl").read_text().splitlines()]
                wire_requests = [event["body"] for event in wire_events if "body" in event]
                self.assertEqual(native_requests, wire_requests)
                self.assertEqual(native_requests, requests)
                self.assertIn("You are running a Roller task", sft["messages"][0]["content"])
                self.assertTrue(any(m.get("reasoning_content") for m in sft["messages"]))
                self.assertFalse((sample / "cache_trace.jsonl").exists())
                self.assertNotIn("test-local-secret", raw)
                before = len(requests)
                self.assertEqual(_collect(_build_parser().parse_args(command + ["--resume"])), 0)
                self.assertEqual(len(requests), before)
                self.assertEqual((sample / "trace.jsonl").read_text(), raw)
                with self.assertRaisesRegex(Exception, "lock|configuration|mismatch"):
                    _collect(_build_parser().parse_args(command + ["--resume", "--pi-max-steps", "3"]))
                # A second native session verifies export-only reasoning removal
                # and schema capture without the HTTP proxy.
                harness = PiHarness(PiConfig(
                    api_base=f"http://127.0.0.1:{server.server_port}/v1", api_key_env="ROLLER_LOCAL_KEY",
                    docker_image=os.environ.get("ROLLER_PI_IMAGE", DEFAULT_PI_IMAGE),
                    wire_trace_mode="off", keep_thinking=False, context_length=128000,
                ), 0)
                destination = root / "no-thinking"
                result = harness.run(RunRequest(0, Task("reasoning", ("Reply DONE",), 0, harness="pi"), "roller-test", 120), destination, 1)
                self.assertEqual(result.status, "ok", result.error)
                trace = json.loads((destination / result.trajectory_path).read_text())
                self.assertTrue(trace["tools"])
                self.assertTrue(all(s["assistant_content"]["reasoning_content"] == "" for s in trace["steps"]))
                self.assertIn("The requested work is complete.", (destination / result.trace_path).read_text())
                self.assertFalse((destination / "wire_trace.jsonl").exists())
            self.assertGreaterEqual(len(requests), 3)
            self.assertTrue(any(m.get("role") == "assistant" and m.get("content") == "DONE" for m in requests[2]["messages"]))

    def test_timeout_recovers_partial_outputs_and_removes_container(self):
        with tempfile.TemporaryDirectory(prefix="roller-pi-timeout-") as tmp, patch.dict(os.environ, {"ROLLER_LOCAL_KEY": "test-local-secret"}):
            harness = PiHarness(PiConfig(
                api_base="http://127.0.0.1:1/v1", api_key_env="ROLLER_LOCAL_KEY", wire_trace_mode="off",
                docker_image=os.environ.get("ROLLER_PI_IMAGE", DEFAULT_PI_IMAGE),
            ), 0)
            task = Task("timeout", ("wait",), 0, harness="pi", setup_commands=("printf PARTIAL > outputs/partial.txt",))
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

    def test_api_error_is_not_a_successful_trajectory(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                data = json.dumps({"error": {"type": "invalid_request_error", "message": "LOCAL_TEST_REJECTION 测试拒绝"}}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(prefix="roller-pi-api-error-") as tmp, patch.dict(os.environ, {"ROLLER_LOCAL_KEY": "test-local-secret"}):
            harness = PiHarness(PiConfig(
                api_base=f"http://127.0.0.1:{server.server_port}/v1", api_key_env="ROLLER_LOCAL_KEY",
                docker_image=os.environ.get("ROLLER_PI_IMAGE", DEFAULT_PI_IMAGE),
            ), 0)
            task = Task("api-error", ("Reply DONE",), 0, harness="pi", setup_commands=("printf PARTIAL > outputs/partial.txt",))
            result = harness.run(RunRequest(0, task, "roller-test", 120), Path(tmp), 1)
            self.assertEqual(result.status, "error", result.error)
            self.assertIsNone(result.trajectory_path)
            self.assertEqual((Path(tmp) / "results/partial.txt").read_text(), "PARTIAL")
            self.assertTrue(requests)
            raw = (Path(tmp) / result.trace_path).read_text()
            self.assertIn("LOCAL_TEST_REJECTION", raw)
            self.assertIn("测试拒绝", result.error)
            self.assertNotIn("test-local-secret", raw)


if __name__ == "__main__":
    unittest.main()
