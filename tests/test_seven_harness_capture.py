import json
import gzip
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from roller.harnesses.codex import CodexConfig, CodexHarness

from roller.bridge_capture import read_bridge_native
from roller.capture import CallRecorder, read_jsonl, validate_capture
from roller.capture_protocol import NativeCapture, finalize_capture
from roller.codex_capture import read_codex_native, retain_root_tools
from roller.codex_tools import tool_snapshot, read_tool_capture
from roller.capture_runtime import CaptureRuntime
from roller.model_protocols import decode_response, response_finished, tool_call_ids, tool_result_ids
from roller.wire_trace import WireTraceProxy
from roller.openclaw_capture import read_openclaw_native
from roller.storage import write_jsonl


class SevenHarnessCaptureTests(unittest.TestCase):
    def test_responses_preserves_encrypted_reasoning_and_tool_references(self):
        response = {"object": "response", "id": "r1", "model": "m", "status": "completed", "output": [
            {"type": "reasoning", "encrypted_content": "opaque", "summary": []},
            {"type": "function_call", "call_id": "t1", "name": "spawn_agent", "arguments": "{}"}]}
        data = b'data: ' + json.dumps({"type": "response.completed", "response": response}).encode() + b'\n\n'
        output = decode_response("openai-responses", data, "text/event-stream")
        self.assertEqual(output, response)
        self.assertTrue(response_finished("openai-responses", output))
        self.assertEqual(tool_call_ids("openai-responses", output), {"t1"})
        self.assertEqual(tool_result_ids("openai-responses", {"input": [
            {"type": "function_call_output", "call_id": "t1", "output": "done"}]}), {"t1"})
        with self.assertRaises(ValueError):
            decode_response("openai-responses", b'data: {"type":"response.created"}\n\n', "text/event-stream")

    def test_native_header_is_forwarded_and_protocol_follows_actual_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            proxy = WireTraceProxy("http://model.invalid", directory / "wire.jsonl", capture_identity_header="thread-id")
            self.assertNotIn("thread-id", proxy.drop_request_headers)
            recorder = CallRecorder(directory)
            body = {"model": "m", "input": []}
            recorder.begin(1, "/v1/responses", body, native_identity={"session_id": "root"})
            response = {"id": "r", "object": "response", "model": "m", "status": "completed", "output": []}
            recorder.response_path(1).write_text(json.dumps(response))
            recorder.finish(1, status=200, content_type="application/json", content_encoding="", complete=True)
            write_jsonl(directory / "native/root.jsonl", [{"id": "root"}])
            native = NativeCapture("openclaw", "openai-chat-completions")
            native.participants["root"] = {"participant_id": "root", "role": "root", "identity_status": "observed", "native_files": ["native/root.jsonl"]}
            native.session_owners["root"] = {"root"}
            native.expected_call_counts["root"] = 1
            result = finalize_capture(directory, native, execution_status="ok")
            self.assertEqual(result["capture_status"], "complete")
            self.assertEqual(read_jsonl(directory / "calls.jsonl")[0]["protocol"], "openai-responses")

    def test_parallel_hermes_identity_and_one_batch_to_two_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "capture/attempt-0001"
            recorder = CallRecorder(directory)
            function = {"id": "batch", "type": "function", "function": {"name": "delegate_task", "arguments": "{}"}}
            for seq, sid in enumerate(("root", "b", "a", "root"), 1):
                body = {"model": "m", "messages": [{"role": "user", "content": "same prompt"}]}
                if seq == 4:
                    body["messages"].append({"role": "tool", "tool_call_id": "batch", "content": "both results"})
                # IDs collide across children; request identity must win.
                output = {"id": "same" if sid != "root" else f"r{seq}", "model": "m", "choices": [
                    {"index": 0, "finish_reason": "tool_calls" if seq == 1 else "stop", "message": {
                        "role": "assistant", "content": "done", **({"tool_calls": [function]} if seq == 1 else {})}}]}
                recorder.begin(seq, "/v1/chat/completions", body, native_identity={"session_id": sid})
                recorder.response_path(seq).write_text(json.dumps(output))
                recorder.finish(seq, status=200, content_type="application/json", content_encoding="", complete=True)
            write_jsonl(directory / "native/root.jsonl", [
                {"type": "participant", "session_id": "root"},
                {"type": "response", "response": {"id": "r1"}},
                {"type": "response", "response": {"id": "r4"}},
                {"type": "message", "message": {"role": "tool", "tool_call_id": "batch", "content": "both results"}}])
            for sid in ("a", "b"):
                write_jsonl(directory / f"native/{sid}.jsonl", [
                    {"type": "participant", "session_id": sid, "parent_session_id": "root", "tool_call_id": "batch"},
                    {"type": "response", "response": {"id": "same"}}, {"type": "child_end", "completed": True}])
            native = read_bridge_native(directory, "hermes")
            result = finalize_capture(directory, native, execution_status="ok")
            self.assertEqual(result["capture_status"], "complete")
            calls = read_jsonl(directory / "calls.jsonl")
            self.assertEqual([c["participant_id"] for c in calls], ["session:root", "session:b", "session:a", "session:root"])
            relations = read_jsonl(directory / "relations.jsonl")
            self.assertEqual(sum(r["type"] == "result_in_context" for r in relations), 2)
            validate_capture(Path(tmp), {"manifest": "capture/attempt-0001/manifest.json", "capture_status": "complete"})

    def test_codex_threads_and_wait_are_distinct_from_launch_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            write_jsonl(directory / "native/root.jsonl", [
                {"type": "session_meta", "payload": {"id": "root"}},
                {"type": "response_item", "payload": {"type": "function_call", "name": "spawn_agent", "call_id": "s"}},
                {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "s", "output": '{"agent_id":"child"}'}},
                {"type": "response_item", "payload": {"type": "function_call", "name": "wait", "call_id": "w"}},
                {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "w", "output": '{"status":{"child":{"completed":"done"}}}'}}])
            write_jsonl(directory / "native/child.jsonl", [{"type": "session_meta", "payload": {"id": "child", "parent_thread_id": "root"}}])
            native = read_codex_native(directory)
            self.assertFalse(native.background_detected)
            self.assertEqual(native.participants["thread:child"]["role"], "child")
            self.assertEqual(next(r["tool_call_id"] for r in native.relations if r["type"] == "launch_acknowledged"), "s")
            self.assertEqual(next(r["tool_call_id"] for r in native.relations if r["type"] == "result_delivered"), "w")

    def test_codex_main_tools_exclude_child_snapshots_without_inventing_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            capture = CaptureRuntime(base, 1, "codex")
            trace = base / "root.jsonl"
            write_jsonl(trace, [{"type": "session_meta", "payload": {"id": "root"}}])
            rows = []
            for seq, sid in ((1, "root"), (2, "child"), (3, "root")):
                body = {"model": "m", "input": [], "tools": [{"type": "function", "name": sid, "parameters": {"type": "object"}}]}
                capture.recorder.begin(seq, "/responses", body, native_identity={"session_id": sid})
                rows.append({"kind": "codex_tools", "seq": seq, "snapshot": tool_snapshot(body)})
            path = base / "tools_capture.jsonl"
            write_jsonl(path, [*rows, {"kind": "codex_tools_complete", "request_count": 3}])
            retain_root_tools(capture, trace)
            recorded = read_jsonl(path)
            self.assertEqual([r["seq"] for r in recorded if r["kind"] == "codex_tools"], [1, 3])
            self.assertEqual(recorded[-1]["request_count"], 2)
            self.assertEqual(len(read_jsonl(capture.recorder.directory / "native/all-tools.jsonl")), 4)
            write_jsonl(path, rows)
            retain_root_tools(capture, trace)
            self.assertFalse(any(r["kind"] == "codex_tools_complete" for r in read_jsonl(path)))
            # Filtering root snapshots must not hide a lost child record.
            write_jsonl(path, [rows[0], rows[2], {"kind": "codex_tools_complete", "request_count": 3}])
            retain_root_tools(capture, trace)
            self.assertEqual(read_tool_capture(path)["status"], "invalid")

    def test_codex_capture_resume_requires_confirmed_root_thread(self):
        harness = CodexHarness(CodexConfig(api_base="http://model.invalid/v1", capture_calls=True), worker_slot=0)
        request = SimpleNamespace(model="m", task=SimpleNamespace(thinking=None))
        with self.assertRaisesRegex(RuntimeError, "confirmed root thread"):
            harness._codex_command("container", request, "next", resume=True)
        harness._root_thread_id = "root-thread"
        command = harness._codex_command("container", request, "next", resume=True)
        self.assertEqual(command[command.index("resume") + 1], "root-thread")
        self.assertNotIn("--last", command)

    def test_bridge_profiles_declare_no_native_subagents(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for harness in ("pi", "deepseek"):
                write_jsonl(directory / "native/trace.jsonl", [{"type": "metadata", "session_id": "root"}])
                native = read_bridge_native(directory, harness)
                self.assertEqual(native.capabilities["subagents"], "not_available_in_bundled_profile")
                self.assertEqual(native.participants["session:root"]["role"], "root")

    def test_openclaw_notification_and_native_id_alias_are_not_launch_completion(self):
        for protocol in ("chat", "responses"):
            with tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp) / "capture/attempt-0001"
                recorder = CallRecorder(directory)
                key = "agent:main:subagent:child"
                native_tool_id = "raw_t1|fc_t1" if protocol == "responses" else "raw_t1"
                field = "input" if protocol == "responses" else "messages"
                notification = f"source: subagent\nsession_key: {key}\nChild result: DONE"
                for seq, sid in ((1, "root"), (2, "child"), (3, "root")):
                    body = {"model": "m", "messages": [{"role": "user", "content": "task"}]}
                    if seq == 3:
                        body["messages"] += [{"role": "tool", "tool_call_id": "rawt1", "content": "accepted"},
                                             {"role": "user", "content": notification}]
                    message = {"role": "assistant", "content": "DONE"}
                    if seq == 1:
                        message["tool_calls"] = [{"id": "raw_t1", "type": "function", "function": {
                            "name": "sessions_spawn", "arguments": "{}"}}]
                    output = {"id": f"r{seq}", "model": "m", "choices": [{"index": 0, "message": message,
                        "finish_reason": "tool_calls" if seq == 1 else "stop"}]}
                    endpoint = "/v1/chat/completions"
                    if protocol == "responses":
                        body["input"] = body.pop("messages")
                        if seq == 3:
                            body["input"][1] = {"type": "function_call_output", "call_id": "raw_t1", "output": "accepted"}
                        output = {"id": f"r{seq}", "object": "response", "model": "m", "status": "completed", "output": [
                            {"type": "function_call", "id": "fc_t1", "call_id": "raw_t1", "name": "sessions_spawn", "arguments": "{}"}
                        ] if seq == 1 else [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "DONE"}]}]}
                        endpoint = "/v1/responses"
                    recorder.begin(seq, endpoint, body, native_identity={"session_id": sid})
                    # Test gzip decoding without rewriting the raw evidence.
                    recorder.response_path(seq).write_bytes(gzip.compress(json.dumps(output).encode()))
                    recorder.finish(seq, status=200, content_type="application/json", content_encoding="gzip", complete=True)
                write_jsonl(directory / "native/stream-contexts.jsonl", [
                    {"sessionId": "root", "sessionKey": "agent:main:explicit:root", "runId": "parent", "stage": "stream:context"},
                    {"sessionId": "child", "sessionKey": key, "runId": "run", "stage": "stream:context"},
                    {"sessionId": "root", "sessionKey": "agent:main:explicit:root", "runId": f"announce:v1:{key}:run",
                     "stage": "stream:context", "messages": [{"role": "user", "content": notification}]}])
                write_jsonl(directory / "native/tool-id-aliases.jsonl", [{"original_id": native_tool_id, "context_id": "rawt1"}])
                (directory / "native/subagent-runs.json").write_text(json.dumps([
                    {"runId": "run", "childSessionKey": key, "delivery": {"status": "delivered"}}]))
                write_jsonl(directory / "native/sessions/root.jsonl", [
                    {"type": "session", "id": "root"}, {"type": "message", "id": "ack", "message": {
                        "role": "toolResult", "toolName": "sessions_spawn", "toolCallId": native_tool_id,
                        "details": {"status": "accepted", "childSessionKey": key, "runId": "run"}}}])
                write_jsonl(directory / "native/sessions/child.jsonl", [
                    {"type": "session", "id": "child"}, {"type": "message", "message": {
                        "role": "assistant", "content": [{"type": "text", "text": "DONE"}], "stopReason": "stop"}}])
                native = read_openclaw_native(directory)
                manifest = finalize_capture(directory, native, execution_status="ok")
                self.assertEqual(manifest["capture_status"], "complete")
                relations = read_jsonl(directory / "relations.jsonl")
                for kind in ("launch_ack_in_context", "result_in_context"):
                    found = [r for r in relations if r["type"] == kind]
                    self.assertEqual(len(found), 1)
                    self.assertTrue(found[0]["call_id"].endswith("call-00000003"))
                validate_capture(Path(tmp), {"manifest": "capture/attempt-0001/manifest.json", "capture_status": "complete"})
                if protocol == "responses":
                    # Matching only call_id would falsely link a different item.
                    native = read_openclaw_native(directory)
                    native.relations[0]["model_tool_ref"]["item_id"] = "wrong-item"
                    finalize_capture(directory, native, execution_status="ok")
                    self.assertTrue(all(r["status"] == "unknown" for r in read_jsonl(directory / "relations.jsonl") if r["type"] == "spawn"))
                # A delivered notification without the exact message in a later
                # request must never use the launch acknowledgement as evidence.
                rows = read_jsonl(recorder.events)
                rows[-2]["body"][field].pop()
                write_jsonl(recorder.events, rows)
                finalize_capture(directory, read_openclaw_native(directory), execution_status="ok")
                self.assertFalse(any(r["type"] == "result_in_context" for r in read_jsonl(directory / "relations.jsonl")))
                (directory / "native/subagent-runs.json").write_text(json.dumps([
                    {"runId": "run", "endedAt": 1, "outcome": {"status": "error"},
                     "delivery": {"status": "delivered"}}]))
                native = read_openclaw_native(directory)
                self.assertFalse(native.background_detected)
                self.assertTrue(any(r["type"] == "child_failed" for r in native.relations))
                self.assertFalse(any(r["type"] == "child_completed" for r in native.relations))


if __name__ == "__main__":
    unittest.main()
