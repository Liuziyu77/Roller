from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from roller.capture import CallRecorder, read_jsonl, validate_capture
from roller.model_protocols import chat_response
from roller.opencode_capture import finalize_opencode_capture
from roller.opencode_actf import opencode_events_to_actf
from roller.sft import actf_to_sft
from roller.storage import write_jsonl


def response(mid, text, tools=None):
    return {"id": mid, "model": "model", "choices": [{"index": 0,
        "message": {"role": "assistant", "content": text,
                    **({"tool_calls": tools} if tools else {})},
        "finish_reason": "tool_calls" if tools else "stop"}]}


class SharedProtocolTests(unittest.TestCase):
    def capture(self, base):
        directory = base / "capture/attempt-0001"
        recorder = CallRecorder(directory)
        task_schema = {"type": "function", "function": {"name": "task", "parameters": {"type": "object"}}}
        tools = [{"id": "ta", "type": "function", "function": {"name": "task", "arguments": '{"prompt":"same"}'}},
                 {"id": "tb", "type": "function", "function": {"name": "task", "arguments": '{"prompt":"same"}'}}]
        rows = []
        for sid, parent in (("root", None), ("a", "root"), ("b", "root")):
            rows.append({"type": "event", "event": {"type": "session.created", "properties": {"info": {"id": sid, "parentID": parent}}}})
        root_parts = [{"id": "p"+sid, "messageID": "r1", "type": "tool", "tool": "task", "callID": "t"+sid,
            "state": {"status": "completed", "input": {"prompt": "same"}, "output": "child "+sid,
                      "metadata": {"sessionId": sid}, "time": {"end": 2}}} for sid in ("a", "b")]
        root_messages = [
            {"info": {"id": "u", "sessionID": "root", "role": "user", "time": {"created": 1}}, "parts": [{"id": "pu", "messageID": "u", "type": "text", "text": "same"}]},
            {"info": {"id": "r1", "sessionID": "root", "role": "assistant", "finish": "tool-calls", "time": {"created": 2, "completed": 3}}, "parts": root_parts},
            {"info": {"id": "r2", "sessionID": "root", "role": "assistant", "finish": "stop", "time": {"created": 4, "completed": 5}}, "parts": [{"id": "pr", "messageID": "r2", "type": "text", "text": "root done"}]},
        ]
        rows.append({"type": "session_export", "data": {"info": {"id": "root"}, "messages": root_messages}})
        for sid in ("a", "b"):
            rows.append({"type": "session_export", "data": {"info": {"id": sid, "parentID": "root"}, "messages": [
                {"info": {"id": "m"+sid, "sessionID": sid, "role": "assistant", "finish": "stop", "time": {"created": 3, "completed": 4}},
                 "parts": [{"id": "p"+sid+"text", "messageID": "m"+sid, "type": "text", "text": "child "+sid}]}]}})
        # Provider response IDs deliberately collide between children. The
        # exact request session ID proves ownership despite identical prompts.
        for seq, sid, output in ((1, "root", response("spawn", "", tools)), (2, "b", response("same", "child b")),
                                 (3, "a", response("same", "child a")), (4, "root", response("final", "root done"))):
            body = {"model": "model", "messages": [{"role": "system", "content": "root system"}, {"role": "user", "content": "same"}], "tools": [task_schema]}
            if seq == 4:
                body["messages"] += [{"role": "tool", "tool_call_id": "ta", "content": "child a"}, {"role": "tool", "tool_call_id": "tb", "content": "child b"}]
            recorder.begin(seq, "/v1/chat/completions", body, native_identity={"session_id": sid})
            recorder.response_path(seq).write_text(json.dumps(output))
            recorder.finish(seq, status=200, content_type="application/json", content_encoding="", complete=True)
            rows.append({"type": "request", "native_session_id": sid, "body": body})
        write_jsonl(directory / "native/journal.jsonl", rows)
        return directory, rows

    def test_parallel_same_prompt_and_response_ids_use_exact_session_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); directory, _ = self.capture(base)
            m = finalize_opencode_capture(directory, execution_status="ok")
            self.assertEqual(m["capture_status"], "complete")
            self.assertEqual([c["participant_id"] for c in read_jsonl(directory/"calls.jsonl")], ["root:root", "agent:b", "agent:a", "root:root"])
            relations = read_jsonl(directory/"relations.jsonl")
            self.assertEqual(sum(r["type"] == "result_in_context" for r in relations), 2)
            self.assertTrue(all(r["parent_call_id"] for r in relations))
            validate_capture(base, {"manifest": "capture/attempt-0001/manifest.json", "capture_status": "complete"})

    def test_missing_identity_preserves_raw_call_and_excludes_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory, _ = self.capture(Path(tmp))
            events = read_jsonl(directory/"events.jsonl")
            next(e for e in events if e["type"] == "request" and e["request_seq"] == 2).pop("native_identity")
            write_jsonl(directory/"events.jsonl", events)
            m = finalize_opencode_capture(directory, execution_status="ok")
            self.assertEqual(m["capture_status"], "partial")
            call = read_jsonl(directory/"calls.jsonl")[1]
            self.assertEqual(call["export_status"], "unavailable")
            self.assertIsNotNone(call["output"])

    def test_native_output_without_http_call_is_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory, _ = self.capture(Path(tmp))
            events = [e for e in read_jsonl(directory/"events.jsonl") if e["request_seq"] != 2]
            write_jsonl(directory/"events.jsonl", events)
            m = finalize_opencode_capture(directory, execution_status="ok")
            self.assertEqual(next(p for p in m["participants"] if p["participant_id"] == "agent:b")["capture_status"], "partial")

    def test_background_receipt_is_not_a_completed_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory, rows = self.capture(Path(tmp))
            rows[3]["data"]["messages"][1]["parts"][0]["state"]["metadata"]["background"] = True
            write_jsonl(directory/"native/journal.jsonl", rows)
            m = finalize_opencode_capture(directory, execution_status="ok")
            self.assertEqual(m["capture_status"], "partial")
            rel = [r for r in read_jsonl(directory/"relations.jsonl") if r["tool_call_id"] == "ta"]
            self.assertNotIn("child_completed", [r["type"] for r in rel])
            self.assertIn("launch_ack_in_context", [r["type"] for r in rel])

    def test_child_requests_and_messages_do_not_change_root_sft(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, rows = self.capture(Path(tmp))
            for row in rows:
                if row.get("type") == "request" and row.get("native_session_id") != "root":
                    row["body"]["messages"][0]["content"] = "child private system"
            sft = actf_to_sft(opencode_events_to_actf(rows))
            self.assertNotIn("child private system", json.dumps(sft))
            assistants = [m for m in sft["messages"] if m["role"] == "assistant"]
            self.assertEqual(len(assistants), 2)
            self.assertEqual(assistants[-1]["content"], "root done")

    def test_capture_error_preserves_execution_and_complete_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory, rows = self.capture(Path(tmp))
            rows.append({"type": "capture_error", "error": "child session export failed: TimeoutError"})
            write_jsonl(directory/"native/journal.jsonl", rows)
            m = finalize_opencode_capture(directory, execution_status="ok")
            self.assertEqual(m["execution_status"], "ok")
            self.assertEqual(m["capture_status"], "partial")
            self.assertEqual(len(m["native_errors"]), 1)
            self.assertTrue(all(c["export_status"] == "available" for c in read_jsonl(directory/"calls.jsonl")))

    def test_response_without_id_is_partial_instead_of_finalization_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory, _ = self.capture(Path(tmp))
            path = directory/"responses/call-00000002.body"
            output = json.loads(path.read_text()); output.pop("id"); path.write_text(json.dumps(output))
            m = finalize_opencode_capture(directory, execution_status="ok")
            self.assertEqual(m["capture_status"], "partial")
            self.assertEqual(read_jsonl(directory/"calls.jsonl")[1]["export_status"], "unavailable")

    def test_unlinked_parent_relation_cannot_claim_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory, _ = self.capture(Path(tmp))
            path = directory/"responses/call-00000001.body"
            output = json.loads(path.read_text()); output["choices"][0]["message"]["tool_calls"][0]["id"] = "different"
            path.write_text(json.dumps(output))
            m = finalize_opencode_capture(directory, execution_status="ok")
            self.assertEqual(m["capture_status"], "partial")
            self.assertTrue(any(r["status"] == "unknown" for r in read_jsonl(directory/"relations.jsonl")))

    def test_different_child_model_and_tool_config_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory, _ = self.capture(Path(tmp))
            events = read_jsonl(directory/"events.jsonl")
            body = next(e for e in events if e["type"] == "request" and e["request_seq"] == 2)["body"]
            body["model"] = "child-model"; body["tools"] = []
            write_jsonl(directory/"events.jsonl", events)
            path = directory/"responses/call-00000002.body"
            output = json.loads(path.read_text()); output["model"] = "child-model"; path.write_text(json.dumps(output))
            finalize_opencode_capture(directory, execution_status="ok")
            call = read_jsonl(directory/"calls.jsonl")[1]
            self.assertEqual(call["context"]["model"], "child-model")
            self.assertEqual(call["context"]["tools"], [])
            self.assertEqual(call["export_status"], "available")

    def test_nested_evidence_keeps_immediate_parent_instead_of_flattening(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory, rows = self.capture(Path(tmp))
            a_export = next(r for r in rows if r.get("type") == "session_export" and r["data"]["info"]["id"] == "a")
            a_message = a_export["data"]["messages"][0]
            a_message["info"]["finish"] = "tool-calls"
            a_message["parts"] = [{"id": "nested-part", "messageID": "ma", "type": "tool", "tool": "task", "callID": "tc",
                "state": {"status": "completed", "input": {"prompt": "grandchild"}, "output": "grandchild result",
                          "metadata": {"sessionId": "c"}, "time": {"end": 4}}}]
            a_export["data"]["messages"].append({"info": {"id": "ma-final", "sessionID": "a", "role": "assistant", "finish": "stop", "time": {"created": 4, "completed": 5}},
                "parts": [{"id": "pa-final", "messageID": "ma-final", "type": "text", "text": "child a"}]})
            rows.append({"type": "session_export", "data": {"info": {"id": "c", "parentID": "a"}, "messages": [
                {"info": {"id": "mc", "sessionID": "c", "role": "assistant", "finish": "stop", "time": {"created": 3, "completed": 4}},
                 "parts": [{"id": "pc", "messageID": "mc", "type": "text", "text": "grandchild result"}]}]}})
            write_jsonl(directory/"native/journal.jsonl", rows)
            events = read_jsonl(directory/"events.jsonl")
            for e in events:
                if e["request_seq"] == 4:
                    e["request_seq"] = 7
                    if e["type"] == "response":
                        e["response_path"] = "responses/call-00000007.body"
            write_jsonl(directory/"events.jsonl", events)
            (directory/"responses/call-00000004.body").rename(directory/"responses/call-00000007.body")
            (directory/"responses/call-00000003.body").write_text(json.dumps(response("a-spawn", "", [
                {"id": "tc", "type": "function", "function": {"name": "task", "arguments": '{"prompt":"grandchild"}'}}])))
            recorder = CallRecorder(directory)
            for seq, sid, output, messages in (
                (4, "c", response("c-final", "grandchild result"), [{"role": "user", "content": "grandchild"}]),
                (5, "a", response("a-final", "child a"), [{"role": "tool", "tool_call_id": "tc", "content": "grandchild result"}]),
            ):
                recorder.begin(seq, "/v1/chat/completions", {"model": "model", "messages": messages}, native_identity={"session_id": sid})
                recorder.response_path(seq).write_text(json.dumps(output))
                recorder.finish(seq, status=200, content_type="application/json", content_encoding="", complete=True)
            m = finalize_opencode_capture(directory, execution_status="ok")
            self.assertEqual(m["capture_status"], "complete")
            spawn = next(r for r in read_jsonl(directory/"relations.jsonl") if r["type"] == "spawn" and r["tool_call_id"] == "tc")
            self.assertEqual(spawn["parent_participant_id"], "agent:a")
            self.assertEqual(spawn["child_participant_id"], "agent:c")


class ChatStreamTests(unittest.TestCase):
    def stream(self, done=True):
        chunks = [
            {"id": "r", "model": "model", "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "plan", "tool_calls": [{"index": 0, "id": "t", "function": {"name": "bash", "arguments": '{"command":'}}]}}]},
            {"id": "r", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"pwd"}'}}]}, "finish_reason": "tool_calls"}]},
            {"id": "r", "choices": [], "usage": {"total_tokens": 4}},
        ]
        return ("".join("data: "+json.dumps(c)+"\n\n" for c in chunks) + ("data: [DONE]\n\n" if done else "")).encode()

    def test_reconstructs_tool_input_reasoning_and_usage(self):
        output = chat_response(self.stream(), "text/event-stream")
        message = output["choices"][0]["message"]
        self.assertEqual(message["reasoning_content"], "plan")
        self.assertEqual(message["tool_calls"][0]["function"]["arguments"], '{"command":"pwd"}')
        self.assertEqual(output["usage"]["total_tokens"], 4)

    def test_finished_choice_without_stream_end_is_partial(self):
        with self.assertRaisesRegex(ValueError, "DONE"):
            chat_response(self.stream(False), "text/event-stream")


if __name__ == "__main__":
    unittest.main()
