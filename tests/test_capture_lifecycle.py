"""Lifecycle regressions: terminal output is not completion delivery evidence."""
import json
from pathlib import Path
import tempfile
import unittest

from roller.capture import CallRecorder, read_jsonl, validate_capture
from roller.capture_protocol import finalize_capture
from roller.claude_capture import read_claude_native
from roller.openclaw_capture import read_openclaw_native
from roller.storage import write_jsonl


class CaptureLifecycleTests(unittest.TestCase):
    def openclaw(self, directory, delivery='delivered', registry=True):
        key = 'agent:main:subagent:child'
        announce = f'announce:v1:{key}:run'
        notification = f'source: subagent\nsession_key: {key}\nChild result: DONE'
        recorder = CallRecorder(directory)
        for seq, sid, run in [(1, 'root', 'parent'), (2, 'child', 'run'), (3, 'root', announce)]:
            # The first request contains copied notification text but belongs
            # to another native run. Only the announcement request proves receipt.
            body = {'model': 'm', 'messages': [{'role': 'user', 'content': notification}]}
            output = {'id': f'r{seq}', 'model': 'm', 'choices': [{'finish_reason': 'stop', 'message': {
                'role': 'assistant', 'content': 'DONE', **({'tool_calls': [{'id': 't', 'type': 'function',
                    'function': {'name': 'sessions_spawn', 'arguments': '{}'}}]} if seq == 1 else {})}}]}
            recorder.begin(seq, '/v1/chat/completions', body, native_identity={'session_id': sid, 'run_id': run})
            recorder.response_path(seq).write_text(json.dumps(output))
            recorder.finish(seq, status=200, content_type='application/json', content_encoding='', complete=True)
        write_jsonl(directory/'native/stream-contexts.jsonl', [
            {'sessionId': sid, 'sessionKey': key if sid == 'child' else 'agent:main:root',
             'runId': run, 'stage': 'stream:context', 'messages': [{'role': 'user', 'content': notification}]}
            for sid, run in [('root', 'parent'), ('child', 'run'), ('root', announce)]])
        if registry:
            (directory/'native/subagent-runs.json').write_text(json.dumps([{
                'runId': 'run', 'childSessionKey': key, 'endedAt': 1, 'outcome': {'status': 'ok'},
                'delivery': {'status': delivery}, 'expectsCompletionMessage': True}]))
        write_jsonl(directory/'native/sessions/root.jsonl', [{'type': 'session', 'id': 'root'},
            {'type': 'message', 'id': 'ack', 'message': {'role': 'toolResult', 'toolName': 'sessions_spawn',
                'toolCallId': 't', 'details': {'status': 'accepted', 'childSessionKey': key, 'runId': 'run'}}}])
        write_jsonl(directory/'native/sessions/child.jsonl', [{'type': 'session', 'id': 'child'},
            {'type': 'message', 'message': {'role': 'assistant', 'stopReason': 'stop'}}])

    def test_pending_notification_is_partial_even_after_child_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp);self.openclaw(d, delivery='pending')
            manifest = finalize_capture(d, read_openclaw_native(d), execution_status='ok', debug_retention='on-error')
            self.assertEqual(manifest['capture_status'], 'partial')
            self.assertTrue(manifest['raw_retained'])
            self.assertEqual(manifest['lifecycle']['pending_participant_ids'], ['session:child'])
            self.assertFalse(any(r['type']=='result_delivered' for r in read_jsonl(d/'relations.jsonl')))

    def test_missing_registry_cannot_prove_background_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp);self.openclaw(d, registry=False)
            m=finalize_capture(d, read_openclaw_native(d), execution_status='ok')
            self.assertEqual(m['capture_status'], 'partial')
            self.assertEqual(m['lifecycle']['status'], 'unknown')

    def test_notification_links_only_its_native_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp);self.openclaw(d)
            m=finalize_capture(d, read_openclaw_native(d), execution_status='ok', debug_retention='on-error')
            self.assertEqual(m['capture_status'], 'complete')
            related=[r for r in read_jsonl(d/'relations.jsonl') if r['type']=='result_in_context']
            self.assertEqual(len(related), 1)
            self.assertTrue(related[0]['call_id'].endswith('call-00000003'))
            validate_capture(d, {'manifest':'manifest.json','capture_status':'complete'})

    def test_nested_claude_metadata_requires_spawn_event_and_known_child(self):
        for variant in ("matched", "wrong_spawn", "unknown_format"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as tmp:
                d = Path(tmp)
                events = [
                    {'type':'assistant','uuid':'root-spawn','sessionId':'root','message':{'id':'r1','content':[{'type':'tool_use','id':'t1','name':'Agent','input':{}}]}},
                    {'type':'assistant','uuid':'child-spawn','sessionId':'root','agentId':'child','isSidechain':True,'message':{'id':'r2','content':[{'type':'tool_use','id':'t2','name':'Agent','input':{}}]}},
                    {'type':'assistant','uuid':'grand-final','sessionId':'root','agentId':'grand','isSidechain':True,'message':{'id':'r3','stop_reason':'end_turn','content':[{'type':'text','text':'GRAND'}]}},
                    {'type':'user','sessionId':'root','toolUseResult':{'agentId':'child','status':'completed'},'message':{'role':'user','content':[{'type':'tool_result','tool_use_id':'t1','content':'CHILD'}]}},
                    {'type':'user','sessionId':'root','agentId':'child','isSidechain':True,'sourceToolAssistantUUID':'wrong' if variant == 'wrong_spawn' else 'child-spawn',
                     'message':{'role':'user','content':[{'type':'tool_result','tool_use_id':'t2','content':[{'type':'text','text':'GRAND'},{'type':'text','text':"agentId: grand (use SendMessage with to: 'grand', summary: '<5-10 word recap>' to continue this agent)\n<usage>subagent_tokens: 1</usage>"}]}]}},
                ]
                if variant == "unknown_format":
                    events[-1]["message"]["content"][0]["content"][-1]["text"] = "agentId: grand (new native metadata format)"
                write_jsonl(d/'native/root.jsonl',events)
                native = read_claude_native(d)
                relation = next(r for r in native.relations if r['type']=='spawn' and r['tool_call_id']=='t2')
                self.assertEqual(relation['parent_participant_id'],'agent:child')
                self.assertEqual(relation.get('child_participant_id'),'agent:grand' if variant == 'matched' else None)
                if variant != "matched":
                    self.assertEqual(relation["status"], "unknown")
                    self.assertIn("metadata does not match", relation["reason"])

    def test_claude_native_notification_closes_background_lifecycle(self):
        for completed, resumed in ((True, False), (False, False), (True, True)):
            with self.subTest(completed=completed, resumed=resumed), tempfile.TemporaryDirectory() as tmp:
                d=Path(tmp);recorder=CallRecorder(d)
                notification='<task-notification><task-id>child</task-id><tool-use-id>t</tool-use-id><status>completed</status><result>DONE</result></task-notification>'
                events=[]
                for seq,pid,output in [(1,'root',{'id':'spawn','content':[{'type':'tool_use','id':'t','name':'Agent','input':{'run_in_background':True}}],'stop_reason':'tool_use'}),
                    (2,'child',{'id':'child-final','content':[{'type':'text','text':'DONE'}],'stop_reason':'end_turn'}),
                    (3,'root',{'id':'parent-final','content':[{'type':'text','text':'DONE'}],'stop_reason':'end_turn'})]:
                    output.update(model='m',type='message',role='assistant')
                    recorder.begin(seq,'/v1/messages',{'model':'m','messages':[{'role':'user','content':('[SYSTEM NOTIFICATION - NOT USER INPUT]\nNative event.\n\n'+notification) if seq==3 else 'task'}]})
                    recorder.response_path(seq).write_text(json.dumps(output));recorder.finish(seq,status=200,content_type='application/json',content_encoding='',complete=True)
                    events.append({'type':'assistant','uuid':f'e{seq}','parentUuid':'notice' if seq==3 and completed else None,
                        'sessionId':'root','isSidechain':pid=='child',**({'agentId':'child'} if pid=='child' else {}),'message':output})
                events.append({'type':'user','uuid':'ack','sessionId':'root','toolUseResult':{'agentId':'child','status':'async_launched'},
                    'message':{'role':'user','content':[{'type':'tool_result','tool_use_id':'t','content':'started'}]}})
                if completed:
                    events.append({'type':'user','uuid':'notice','sessionId':'root','origin':{'kind':'task-notification'},'message':{'role':'user','content':notification}})
                if resumed:
                    events.extend([
                        {'type':'assistant','uuid':'resume','sessionId':'root','message':{'id':'parent-final','content':[{'type':'tool_use','id':'t2','name':'Agent','input':{'resume':'child','run_in_background':True}}]}},
                        {'type':'user','uuid':'ack2','sessionId':'root','toolUseResult':{'agentId':'child','status':'async_launched'},'message':{'role':'user','content':[{'type':'tool_result','tool_use_id':'t2','content':'resumed'}]}}])
                write_jsonl(d/'native/root.jsonl',events)
                m=finalize_capture(d,read_claude_native(d),execution_status='ok')
                self.assertEqual(m['capture_status'],'complete' if completed and not resumed else 'partial')
                relations=read_jsonl(d/'relations.jsonl')
                self.assertEqual(any(r['type']=='result_in_context' for r in relations),completed)
                validate_capture(d,{'manifest':'manifest.json','capture_status':m['capture_status']})
