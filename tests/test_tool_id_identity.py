import unittest
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch
from roller.tool_id_identity import ToolIdRegistry, scope_claude_events
from roller.claude_code_actf import claude_code_events_to_actf
from roller.harnesses.claude_code import ClaudeCodeConfig, ClaudeCodeHarness
from roller.wire_trace import WireTraceProxy, _ProxyHandler


def call(message, native='x'):
    return {'type':'assistant','message':{'id':message,'content':[{'type':'tool_use','id':native,'name':'Bash','input':{'command':'true'}}]}}


def result(native='x'):
    return {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':native,'content':'ok'}]}}


class IdentityTests(unittest.TestCase):
    def test_sequential_calls_exported_with_provenance(self):
        rows=[call('a'),result(),call('b'),result()]
        actf=claude_code_events_to_actf(rows)
        calls=[c for s in actf['steps'] for c in s['assistant_content']['tool_calls']]
        observations=[o for s in actf['steps'] for o in s['observation']]
        self.assertEqual(len(calls),2)
        self.assertEqual([c['id'] for c in calls],[o['toolCallId'] for o in observations])
        self.assertEqual(calls[1]['original_tool_call_id'],'x')
        self.assertEqual(rows[2]['message']['content'][0]['id'],'x')

    def test_repeated_message_fragment_after_result(self):
        rows=scope_claude_events([call('a'),result(),call('a'),call('b'),result()])
        self.assertEqual(rows[3]['message']['content'][0]['id'],'x__occurrence_2')

    def test_overlap_rejected(self):
        with self.assertRaisesRegex(ValueError,'ambiguous'):
            scope_claude_events([call('a'),call('b'),result()])

    def test_generated_identity_never_shadows_native(self):
        rows=scope_claude_events([call('a'),result(),call('b'),result(),call('c','x__occurrence_2')])
        self.assertEqual(rows[2]['message']['content'][0]['id'],'x__occurrence_3')

    def test_missing_message_ids_are_distinct(self):
        rows=[call(None),result(),call(None),result()]
        self.assertNotEqual(scope_claude_events(rows)[2]['message']['content'][0]['id'],'x')

    def test_runtime_collision_only_and_input_immutable(self):
        registry=ToolIdRegistry(); mapping=[]
        event={'type':'content_block_start','content_block':call('a')['message']['content'][0]}
        first=registry.response(event,mapping); second=registry.response(event,mapping)
        self.assertEqual(first,event)
        self.assertNotEqual(second['content_block']['id'],'x')
        self.assertEqual(mapping,[{'native_id':'x','delivered_id':second['content_block']['id']}])
        msg={'type':'message','content':[{'type':'tool_use','id':'y'}]}
        self.assertEqual(registry.response(msg,mapping),msg)

    def test_audit_mapping_survives_pruning(self):
        import tempfile
        from pathlib import Path
        from roller.collector import Collector
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            mapping=root/'tool_id_mappings.jsonl'
            mapping.write_text('{}\n')
            Collector._prune_sample(root)
            self.assertTrue(mapping.exists())

    def test_explicit_repair_starts_proxy_even_with_logging_off(self):
        with tempfile.TemporaryDirectory() as tmp, patch("roller.harnesses.claude_code.WireTraceProxy") as proxy:
            config = ClaudeCodeConfig(api_base="http://example.test", wire_trace_mode="off", unique_tool_ids=True)
            ClaudeCodeHarness(config, 0, runtime=MagicMock())._start_wire_proxy(Path(tmp))
            self.assertEqual(proxy.call_args.kwargs["mode"], "off")
            self.assertTrue(proxy.call_args.kwargs["unique_anthropic_tool_ids"])
            proxy.reset_mock()
            config = ClaudeCodeConfig(api_base="http://example.test", wire_trace_mode="off")
            ClaudeCodeHarness(config, 0, runtime=MagicMock())._start_wire_proxy(Path(tmp))
            proxy.assert_not_called()

    def test_http_json_and_sse_are_repaired_before_cli_receives_them(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                proxy = WireTraceProxy("http://example.test", root / "wire.jsonl", mode="off", unique_anthropic_tool_ids=True)
                event = ({"type": "content_block_start", "content_block": {"type": "tool_use", "id": "x"}}
                         if streaming else {"type": "message", "content": [{"type": "tool_use", "id": "x"}]})
                outputs = []
                for _ in range(2):
                    body = json.dumps(event).encode()
                    response = io.BytesIO(b"data: " + body + b"\n\n" if streaming else body)
                    response.status = 200
                    response.headers = {"Content-Type": "text/event-stream" if streaming else "application/json"}
                    handler = MagicMock()
                    handler.server.proxy = proxy
                    handler.path, handler.command = "/v1/messages", "POST"
                    handler.headers = {"Content-Length": "2"}
                    handler.rfile, handler.wfile = io.BytesIO(b"{}"), io.BytesIO()
                    with patch("roller.wire_trace.urllib_request.urlopen", return_value=response):
                        _ProxyHandler._handle(handler)
                    raw = handler.wfile.getvalue()
                    outputs.append(json.loads(raw[6:].strip() if streaming else raw))
                def identifier(event):
                    return event["content_block"]["id"] if streaming else event["content"][0]["id"]
                self.assertEqual(identifier(outputs[0]), "x")
                self.assertNotEqual(identifier(outputs[1]), "x")
                self.assertTrue((root / "tool_id_mappings.jsonl").is_file())
                self.assertFalse((root / "wire.jsonl").exists())
