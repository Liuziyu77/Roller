from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from roller.capture import CallRecorder, read_jsonl, validate_capture
from roller.claude_capture import anthropic_response, finalize_claude_capture
from roller.claude_code_actf import claude_code_events_to_actf
from roller.sft import SFTConversionError, actf_to_sft
from roller.storage import write_jsonl
from roller.collector import CollectionConfig, Collector
from roller.harnesses.base import Harness
from roller.models import HarnessResult, RunRequest, Task


def message(mid, content, stop="end_turn"):
    return {"type": "message", "id": mid, "model": "model", "role": "assistant",
            "content": content, "stop_reason": stop, "usage": {"output_tokens": 3}}


def event(mid, content, *, agent=None, kind="assistant", **extra):
    return {"uuid": mid, "type": kind, "sessionId": "shared-session",
            "agentId": agent, "isSidechain": agent is not None,
            "message": message(mid, content), **extra}


class CaptureTests(unittest.TestCase):
    def make_capture(self, base: Path):
        directory = base / "capture/attempt-0001"
        recorder = CallRecorder(directory)
        native = [event("root-spawn", [{"type":"tool_use","name":"Agent","id":"spawn-a","input":{"prompt":"identical"}},
                                       {"type":"tool_use","name":"Agent","id":"spawn-b","input":{"prompt":"identical"}}]),
                  event("a", [{"type":"text","text":"child A"}], agent="a"),
                  event("b", [{"type":"text","text":"child B"}], agent="b"),
                  event("result-a", [{"type":"tool_result","tool_use_id":"spawn-a","content":"child A"}], kind="user", toolUseResult={"agentId":"a","status":"completed"}),
                  event("result-b", [{"type":"tool_result","tool_use_id":"spawn-b","content":"child B"}], kind="user", toolUseResult={"agentId":"b","status":"completed"}),
                  event("root-final", [{"type":"text","text":"root done"}])]
        # Deliberately reverse the children's response order. Prompt text and
        # session ID are identical; only message IDs prove attribution.
        for seq, mid in enumerate(("root-spawn", "b", "a", "root-final"), 1):
            output = next(e["message"] for e in native if e["uuid"] == mid)
            body = {"model":"model","messages":[{"role":"user","content":"identical"}],"tools":[]}
            if mid == "root-final":
                body["messages"].append({"role":"user","content":[
                    {"type":"tool_result","tool_use_id":"spawn-a","content":"child A"},
                    {"type":"tool_result","tool_use_id":"spawn-b","content":"child B"}]})
            recorder.begin(seq, "/v1/messages", body)
            recorder.response_path(seq).write_text(json.dumps(output))
            recorder.finish(seq,status=200,content_type="application/json",content_encoding="",complete=True)
        write_jsonl(directory / "native/root.jsonl",[e for e in native if not e['isSidechain']])
        write_jsonl(directory / "native/a.jsonl",[e for e in native if e['agentId']=='a'])
        write_jsonl(directory / "native/b.jsonl",[e for e in native if e['agentId']=='b'])
        return directory, native

    def test_parallel_identical_prompts_are_attributed_by_response_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);directory,_=self.make_capture(base)
            manifest=finalize_claude_capture(directory,execution_status="ok")
            calls=read_jsonl(directory/'calls.jsonl')
            self.assertEqual(manifest['capture_status'],'complete')
            self.assertEqual([c['participant_id'] for c in calls],['root:shared-session','agent:b','agent:a','root:shared-session'])
            relations=read_jsonl(directory/'relations.jsonl')
            spawns={r['tool_call_id']:r['child_participant_id'] for r in relations if r['type']=='spawn'}
            self.assertEqual(spawns,{'spawn-a':'agent:a','spawn-b':'agent:b'})
            self.assertEqual(sum(r['type']=='result_in_context' for r in relations),2)
            validate_capture(base,{'manifest':'capture/attempt-0001/manifest.json','capture_status':'complete'})

    def test_compact_capture_replays_without_duplicate_contexts(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            directory, _ = self.make_capture(base)
            manifest = finalize_claude_capture(directory, execution_status="ok")
            before = read_jsonl(directory / "calls.jsonl")
            self.assertEqual({p.name for p in directory.iterdir()},
                             {"manifest.json", "calls.jsonl", "relations.jsonl", "raw"})
            self.assertTrue(all("calls_path" not in p for p in manifest["participants"]))
            events = read_jsonl(directory / "raw/events.jsonl")
            self.assertTrue(all("body" not in e and "body_ref" in e
                                for e in events if e["type"] == "request"))
            manifest = finalize_claude_capture(directory, execution_status="ok")
            self.assertEqual(before, read_jsonl(directory / "calls.jsonl"))
            validate_capture(base, {"manifest": "capture/attempt-0001/manifest.json",
                                    "capture_status": manifest["capture_status"]})

    def test_unmatched_response_is_preserved_but_not_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory,_=self.make_capture(Path(tmp))
            payload=json.loads((directory/'responses/call-00000002.body').read_text())
            payload['id']='rewritten-by-provider'
            (directory/'responses/call-00000002.body').write_text(json.dumps(payload))
            m=finalize_claude_capture(directory,execution_status='ok')
            calls=read_jsonl(directory/'calls.jsonl')
            self.assertEqual(m['capture_status'],'partial')
            self.assertIsNone(calls[1]['participant_id'])
            self.assertEqual(calls[1]['export_status'],'unavailable')
            self.assertEqual(calls[0]['export_status'],'available')

    def test_missing_child_native_file_does_not_claim_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory,_=self.make_capture(Path(tmp))
            (directory/'native/b.jsonl').unlink()
            m=finalize_claude_capture(directory,execution_status='ok')
            child=next(p for p in m['participants'] if p['participant_id']=='agent:b')
            self.assertEqual(child['capture_status'],'partial')
            self.assertEqual(m['capture_status'],'partial')

    def test_manifest_hash_detects_changed_or_missing_native(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);directory,_=self.make_capture(base)
            finalize_claude_capture(directory,execution_status='ok')
            metadata={'manifest':'capture/attempt-0001/manifest.json','capture_status':'complete'}
            (directory/'raw/native/a.jsonl').write_text('{}\n')
            with self.assertRaisesRegex(ValueError,'hash mismatch'):
                validate_capture(base,metadata)
            (directory/'raw/native/a.jsonl').unlink()
            with self.assertRaisesRegex(ValueError,'missing'):
                validate_capture(base,metadata)

    def test_manifest_cannot_reference_outside_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,'escapes'):
                validate_capture(Path(tmp),{'manifest':'../manifest.json'})

    def test_interrupted_response_is_not_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory,_=self.make_capture(Path(tmp))
            recorder=CallRecorder(directory)
            recorder.finish(2,status=200,content_type='application/json',content_encoding='',complete=False,error='connection interrupted')
            m=finalize_claude_capture(directory,execution_status='ok')
            self.assertEqual(m['capture_status'],'partial')
            self.assertEqual(read_jsonl(directory/'calls.jsonl')[1]['export_status'],'unavailable')

    def test_different_attempts_have_distinct_call_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=CallRecorder(Path(tmp)/'attempt-0001')
            b=CallRecorder(Path(tmp)/'attempt-0002')
            self.assertNotEqual(a.begin(1,'/messages',{}),b.begin(1,'/messages',{}))

    def test_sealing_freezes_inflight_response_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder=CallRecorder(Path(tmp)/'attempt-0001')
            recorder.begin(1,'/messages',{})
            stream=recorder.open_response(1)
            recorder.append_response(stream,b'first')
            recorder.seal()
            recorder.append_response(stream,b'late bytes')
            recorder.finish(1,status=200,content_type='application/json',content_encoding='',complete=True)
            stream.close()
            self.assertEqual(recorder.response_path(1).read_bytes(),b'first')
            self.assertEqual(len(read_jsonl(recorder.events)),1)
            self.assertIn('in flight',recorder.error)

    def test_collector_preserves_capture_without_diagnostics_and_resumes(self):
        fixture=self
        state={'runs':0}
        class RecordedHarness(Harness):
            name='claude-code'
            def run(self,request,output_dir,attempt):
                state['runs']+=1
                directory,native=fixture.make_capture(output_dir)
                m=finalize_claude_capture(directory,execution_status='ok')
                raw=[{'type':'user','message':{'content':'identical'}},*native]
                write_jsonl(output_dir/'trace.jsonl',raw)
                (output_dir/'trace_ACTF.json').write_text(json.dumps(claude_code_events_to_actf(raw,user_messages=['identical'])))
                return HarnessResult(status='ok',elapsed_seconds=0,trace_path='trace.jsonl',trajectory_path='trace_ACTF.json',
                    metadata={'capture':{'manifest':'capture/attempt-0001/manifest.json','capture_status':m['capture_status']}})
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp)
            request=RunRequest(0,Task(id='sample',prompts=('identical',),source_index=0),'model',30)
            factory=lambda slot:RecordedHarness()
            results=Collector(CollectionConfig(run_dir=run),factory).collect([request])
            self.assertEqual(results[0]['status'],'ok')
            self.assertEqual(len(read_jsonl(run/'calls.jsonl')),4)
            sample=run/'samples/model/00000000_sample'
            self.assertTrue((sample/'capture/attempt-0001/raw/native/a.jsonl').is_file())
            resumed=Collector(CollectionConfig(run_dir=run,resume=True),factory).collect([request])
            self.assertEqual(resumed[0]['status'],'skipped')
            self.assertEqual(state['runs'],1)
            (sample/'capture/attempt-0001/raw/responses/call-00000002.body').write_text('tampered')
            rerun=Collector(CollectionConfig(run_dir=run,resume=True),factory).collect([request])
            self.assertEqual(rerun[0]['status'],'ok')
            self.assertEqual(state['runs'],2)

    def test_child_model_and_tools_are_not_replaced_with_root_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory,_=self.make_capture(Path(tmp))
            events=read_jsonl(directory/'events.jsonl')
            for row in events:
                if row['type']=='request' and row['request_seq']==2:
                    row['body']['model']='child-model'
                    row['body']['tools']=[{'name':'ChildOnly','input_schema':{'type':'object'}}]
            write_jsonl(directory/'events.jsonl',events)
            path=directory/'responses/call-00000002.body'
            output=json.loads(path.read_text());output['model']='child-model';path.write_text(json.dumps(output))
            finalize_claude_capture(directory,execution_status='ok')
            call=read_jsonl(directory/'calls.jsonl')[1]
            self.assertEqual(call['context']['model'],'child-model')
            self.assertEqual(call['context']['tools'][0]['name'],'ChildOnly')
            self.assertEqual(call['export_status'],'available')


class AnthropicStreamTests(unittest.TestCase):
    def stream(self):
        return [
            {'type':'message_start','message':message('m',[],None)},
            {'type':'content_block_start','index':0,'content_block':{'type':'thinking','thinking':''}},
            {'type':'content_block_delta','index':0,'delta':{'type':'thinking_delta','thinking':'plan'}},
            {'type':'content_block_delta','index':0,'delta':{'type':'signature_delta','signature':'signature'}},
            {'type':'content_block_stop','index':0},
            {'type':'content_block_start','index':1,'content_block':{'type':'tool_use','id':'t','name':'Read','input':{}}},
            {'type':'content_block_delta','index':1,'delta':{'type':'input_json_delta','partial_json':'{"path":'}},
            {'type':'content_block_delta','index':1,'delta':{'type':'input_json_delta','partial_json':'"file"}'}},
            {'type':'content_block_stop','index':1},
            {'type':'message_delta','delta':{'stop_reason':'tool_use'},'usage':{'output_tokens':10}},
            {'type':'message_stop'}]

    def test_stream_preserves_thinking_signature_and_tool_input(self):
        payload=''.join('data: '+json.dumps(e)+'\r\n\r\n' for e in self.stream()).encode()
        response=anthropic_response(payload,'text/event-stream')
        self.assertEqual(response['content'][0],{'type':'thinking','thinking':'plan','signature':'signature'})
        self.assertEqual(response['content'][1]['input'],{'path':'file'})
        self.assertEqual(response['usage']['output_tokens'],10)

    def test_stream_without_stop_is_incomplete(self):
        payload=''.join('data: '+json.dumps(e)+'\n\n' for e in self.stream()[:-1]).encode()
        with self.assertRaisesRegex(ValueError,'message_stop'):
            anthropic_response(payload,'text/event-stream')


class RootProjectionTests(unittest.TestCase):
    def test_child_and_synthetic_are_not_root_assistant_steps(self):
        rows=[{'type':'user','message':{'content':'root prompt'}},
              event('r',[{'type':'text','text':'root'}]),
              event('c',[{'type':'text','text':'child'}],agent='a'),
              {'type':'assistant','message':{'model':'<synthetic>','content':[{'type':'text','text':'No response requested.'}]}}]
        actf=claude_code_events_to_actf(rows,user_messages=['root prompt'])
        sft=actf_to_sft(actf)
        self.assertEqual([m['content'] for m in sft['messages'] if m['role']=='assistant'],['root'])

    def test_compaction_preserves_final_user_but_blocks_linear_sft(self):
        rows=[{'type':'user','message':{'content':'before'}},event('r',[{'type':'text','text':'before answer'}]),
              {'type':'system','subtype':'compact_boundary'},
              {'type':'user','message':{'content':'after'}},event('r2',[{'type':'text','text':'after answer'}])]
        actf=claude_code_events_to_actf(rows,user_messages=['before','/compact','after'])
        self.assertEqual(actf['steps'][-1]['user_content'],'after')
        with self.assertRaisesRegex(SFTConversionError,'compaction'):
            actf_to_sft(actf)


if __name__ == '__main__':
    unittest.main()
