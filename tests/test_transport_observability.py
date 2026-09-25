import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from roller.collector import Collector
from roller.wire_trace import WireTraceProxy, _ProxyHandler


class TransportObservabilityTests(unittest.TestCase):
    def handler(self, base, *, repair=False):
        handler = MagicMock()
        handler.server.proxy = WireTraceProxy("http://example.test", base / "wire.jsonl", mode="off", unique_anthropic_tool_ids=repair)
        handler.headers = {"Content-Length": "2", "Authorization": "Bearer hidden-header"}
        handler.rfile, handler.wfile = io.BytesIO(b"{}"), io.BytesIO()
        handler.path, handler.command = "/secret-prefix/v1/messages?api_key=hidden-query", "POST"
        return handler

    def rows(self, base):
        return [json.loads(line) for line in (base / "transport_trace.jsonl").read_text().splitlines()]

    def test_read_failure_logged_no_replay_response_closed(self):
        for repair in (False, True):
            with self.subTest(repair=repair), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                handler = self.handler(base, repair=repair)
                response = MagicMock(status=200, headers={"Content-Type": "text/event-stream"})
                response.read.side_effect = [b"data: {}\n\n", ConnectionResetError("hidden-body")]
                response.readline.side_effect = [b"data: {}\n\n", ConnectionResetError("hidden-body")]
                with patch("roller.wire_trace.urllib_request.urlopen", return_value=response) as send:
                    _ProxyHandler._handle(handler)
                send.assert_called_once()
                response.close.assert_called_once()
                rows = self.rows(base)
                self.assertEqual(rows[-1]["kind"], "upstream_stream_error")
                self.assertFalse(rows[-1]["will_retry"])
                self.assertEqual(rows[-1]["error_type"], "ConnectionResetError")
                raw = json.dumps(rows)
                for secret in ("hidden-body", "hidden-header", "hidden-query", "secret-prefix"):
                    self.assertNotIn(secret, raw)

    def test_client_disconnect_not_mislabeled_upstream_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            handler = self.handler(base)
            handler.wfile = MagicMock()
            handler.wfile.write.side_effect = BrokenPipeError("sensitive message")
            response = io.BytesIO(b"answer")
            response.status, response.headers = 200, {}
            with patch("roller.wire_trace.urllib_request.urlopen", return_value=response) as send:
                _ProxyHandler._handle(handler)
            send.assert_called_once()
            self.assertTrue(response.closed)
            self.assertEqual(self.rows(base)[-1]["kind"], "client_disconnect")

    def test_initial_timeout_observed_without_replay_and_transport_survives_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            handler = self.handler(base)
            with patch("roller.wire_trace.urllib_request.urlopen", side_effect=TimeoutError("endpoint token")) as send:
                _ProxyHandler._handle(handler)
            send.assert_called_once()
            rows = self.rows(base)
            self.assertEqual(rows[-1]["kind"], "upstream_error")
            self.assertFalse(rows[-1]["will_retry"])
            self.assertNotIn("endpoint token", json.dumps(rows))
            Collector._prune_sample(base)
            self.assertEqual(self.rows(base), rows)

    def test_completed_response_and_mapping_share_request_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            handler = self.handler(base, repair=True)
            event = {"type": "message", "content": [{"type": "tool_use", "id": "same"}]}
            for _ in range(2):
                handler.rfile = io.BytesIO(b"{}")
                response = io.BytesIO(json.dumps(event).encode())
                response.status, response.headers = 200, {"Content-Type": "application/json"}
                with patch("roller.wire_trace.urllib_request.urlopen", return_value=response):
                    _ProxyHandler._handle(handler)
            rows = self.rows(base)
            mapping = json.loads((base / "tool_id_mappings.jsonl").read_text())
            self.assertEqual(mapping["request_seq"], rows[-1]["request_seq"])
            self.assertEqual(rows[-1]["kind"], "response_complete")
            self.assertGreater(rows[-1]["upstream_bytes"], 0)
