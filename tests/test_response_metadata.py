import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from roller.response_metadata import ResponseMetadata
from roller.trajectory_audit import model_identity_audit, record_training_audits
from roller.wire_trace import WireTraceProxy, _ProxyHandler


class ResponseMetadataTests(unittest.TestCase):
    def test_alias_warning_does_not_change_training_eligibility(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "wire.jsonl"
            rows = [
                {"seq": 1, "model": "alias"},
                {"kind": "response_metadata", "request_seq": 1, "response_models": ["actual"], "capture_status": "observed"},
            ]
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            metadata = {}
            with self.assertLogs("roller.trajectory_audit", level="WARNING"):
                record_training_audits(metadata, None, path)
            self.assertNotIn("sft_eligibility", metadata)
            self.assertEqual(metadata["model_identity_audit"]["status"], "name_mismatch")
            rows[1]["response_models"] = ["alias"]
            path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "{broken\n")
            self.assertEqual(model_identity_audit(path)["status"], "not_observable")

    def test_json_and_fragmented_sse_protocols(self):
        cases = [
            {"model": "served", "choices": [{"finish_reason": "length", "message": {"content": "PRIVATE"}}]},
            {"type": "response.incomplete", "response": {"model": "served", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}},
            {"type": "message_start", "message": {"model": "served", "content": "PRIVATE"}},
        ]
        for body in cases:
            for sse in (False, True):
                with self.subTest(body=body, sse=sse):
                    data = json.dumps(body).encode()
                    if sse:
                        data = b"event: message\r\ndata: " + data + b"\r\n\r\ndata: [DONE]\n\n"
                    observer = ResponseMetadata("text/event-stream" if sse else "application/json")
                    for b in data:
                        observer.feed(bytes([b]))
                    result = observer.finish()
                    self.assertEqual(result["response_models"], ["served"])
                    self.assertNotIn("PRIVATE", json.dumps(result))
                    if "response" in body:
                        self.assertEqual(result["response_statuses"], ["incomplete"])
                        self.assertEqual(result["finish_reasons"], ["max_output_tokens"])

    def test_limits_invalid_and_compressed_payloads(self):
        observer = ResponseMetadata("application/json")
        observer.feed(b"x" * (observer.LIMIT + 1))
        self.assertEqual(observer.buffer, b"")
        self.assertEqual(observer.finish()["capture_status"], "partial")
        observer = ResponseMetadata("text/event-stream")
        observer.feed(b"data: " + b"x" * (observer.LIMIT + 1) + b'\ndata: {"model":"served"}\n')
        self.assertEqual(observer.finish()["response_models"], ["served"])
        observer = ResponseMetadata("application/json", "gzip")
        observer.feed(b'{"model":"served"}')
        self.assertEqual(observer.finish()["capture_status"], "not_observable")
        observer = ResponseMetadata("application/json")
        observer.feed(b"\xff")
        self.assertEqual(observer.finish()["capture_status"], "partial")

    def test_proxy_response_bytes_unchanged_and_identity_correlated(self):
        for sse in (False, True):
            with self.subTest(sse=sse), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "wire.jsonl"
                proxy = WireTraceProxy("http://example.test/v1", path)
                payload = b'{"model":"upstream-model","choices":[{"finish_reason":"length","message":{"content":"PRIVATE"}}]}'
                if sse:
                    payload = b"data: " + payload + b"\n\ndata: [DONE]\n\n"
                request = b'{"model":"alias","messages":[]}'
                handler = MagicMock()
                handler.server.proxy = proxy
                handler.path = "/v1/chat/completions"
                handler.command = "POST"
                handler.headers = {"Content-Length": str(len(request))}
                handler.rfile = io.BytesIO(request)
                handler.wfile = io.BytesIO()
                handler._record.side_effect = lambda data: _ProxyHandler._record(handler, data)
                data = io.BytesIO(payload)
                response = SimpleNamespace(status=200, headers={"Content-Type": "text/event-stream" if sse else "application/json"}, read=data.read, readline=data.readline)
                with patch("roller.wire_trace.urllib_request.urlopen", return_value=response):
                    _ProxyHandler._handle(handler)
                self.assertEqual(handler.wfile.getvalue(), payload)
                self.assertNotIn("PRIVATE", path.read_text())
                audit = model_identity_audit(path)
                self.assertEqual(audit["status"], "name_mismatch")
                self.assertEqual(audit["requests"][0]["requested_model"], "alias")
                self.assertEqual(audit["requests"][0]["response_models"], ["upstream-model"])
                self.assertEqual(audit["requests"][0]["finish_reasons"], ["length"])
