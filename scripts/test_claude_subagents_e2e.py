"""Real Docker/service acceptance checks. Credentials come only from an env var.

Example: PYTHONPATH=src python scripts/test_claude_subagents_e2e.py \
  --api-base URL --api-key-env ANTHROPIC_API_KEY --output /tmp/cc-e2e
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from roller.capture import read_jsonl, validate_capture
from roller.cli import _build_parser, _collect


COMMON = 'Use native Agent tools, no research, no web access, no nested delegation. All children must run in foreground with run_in_background=false. '
CASES = {
    'single': (1, COMMON+'Delegate one general-purpose child to return exactly CHILD_SINGLE. Then reply ROOT_SINGLE. Do not use any other tools.'),
    'parallel': (2, COMMON+'In ONE assistant response invoke TWO Agent calls, for A and B, so they run concurrently. Ask A to use Bash to run "sleep 2" then return CHILD_A; ask B to use Bash to run "sleep 2" then return CHILD_B. After both return, reply ROOT_PARALLEL. Do not run Bash yourself.'),
    'identical': (2, COMMON+'In ONE assistant response invoke TWO Agent calls with the SAME prompt: "Return exactly IDENTICAL_CHILD, do not use tools." Use general-purpose for both. After both return, reply ROOT_IDENTICAL. Do not use other tools.'),
    'workspace': (1, COMMON+'Delegate one general-purpose child to use Write to create /roller/workspace/outputs/child.txt containing WORKSPACE_CHILD followed by a newline. Child must write it, then return CHILD_WROTE. Do not write it yourself. After child completes use Read to verify the file, then reply ROOT_WORKSPACE.'),
    'tool-error': (1, COMMON+'Delegate one general-purpose child with this exact instruction: "Use Bash once to execute exit 13. This failure is intentional; do not retry or repair it. After observing the error return CHILD_ERROR_HANDLED." After receiving the child result reply ROOT_TOOL_ERROR. Do not run Bash yourself.'),
    'different-tools': (1, COMMON+'Delegate one Explore child using its native read-only tool profile. Ask it to use Bash for pwd and return CONFIG_CHILD. After it returns, reply ROOT_DIFFERENT_TOOLS. Do not use other tools.'),
    'ordinary': (0, 'Do not use tools or delegate. Reply exactly ROOT_ORDINARY.'),
}


def audit(run: Path, expected_children: int, case: str):
    sample = next((run/'samples').glob('*/*/info.json')).parent
    info = json.loads((sample/'info.json').read_text())
    assert info['status']=='ok', info.get('error')
    capture=info['runtime']['capture']
    validate_capture(sample,capture)
    directory=(sample/capture['manifest']).parent
    manifest=json.loads((directory/'manifest.json').read_text())
    calls=read_jsonl(directory/'calls.jsonl')
    parts=manifest['participants']
    children=[p for p in parts if p['role']=='child']
    assert len(children)==expected_children, parts
    assert all(p['capture_status']=='complete' for p in parts), parts
    # Service-side failed attempts may remain raw/unknown. Every successful
    # model response must be completely captured and uniquely attributed.
    assert all(c['capture_status']=='complete' for c in calls if c['http_status']==200), [(c['request_seq'],c['errors']) for c in calls]
    exported=read_jsonl(run/'calls.jsonl')
    assert len(exported)==sum(c['export_status']=='available' for c in calls)
    sft=json.loads((sample/'trace_SFT.json').read_text())
    tools=[t['function']['name'] for m in sft['messages'] for t in m.get('tool_calls',[])]
    assert set(tools)<=({'Agent','Read'} if case=='workspace' else {'Agent'}), tools
    assert all(m.get('content')!='No response requested.' for m in sft['messages'])
    relations=read_jsonl(directory/'relations.jsonl')
    assert sum(r['type']=='spawn' and r['status']=='observed' for r in relations)==expected_children
    assert sum(r['type']=='child_completed' for r in relations)==expected_children
    assert len({r['tool_call_id'] for r in relations if r['type']=='result_in_context'})==expected_children
    if case=='workspace':
        assert (sample/'results/child.txt').read_text()=='WORKSPACE_CHILD\n'
    if case in {'parallel','identical'}:
        spawns=[c for c in calls if c['output'] and sum(b.get('type')=='tool_use' and b.get('name')=='Agent' for b in c['output'].get('content',[]))==2]
        assert spawns, 'model did not issue two Agent calls in one response'
        intervals=[]
        for p in children:
            cc=[c for c in calls if c['participant_id']==p['participant_id']]
            intervals.append((min(c['started_at'] for c in cc),max(c['finished_at'] for c in cc)))
        assert max(a for a,b in intervals)<min(b for a,b in intervals), 'child execution intervals did not overlap'
    if case=='different-tools':
        root=[c for c in calls if c['participant_id'] and c['participant_id'].startswith('root:')]
        child=[c for c in calls if c['participant_id'] and c['participant_id'].startswith('agent:')]
        assert root[0]['context']['tools']!=child[0]['context']['tools']
    if case=='tool-error':
        child=[c for c in calls if c['participant_id'] and c['participant_id'].startswith('agent:')]
        assert any('exit 13' in json.dumps(c['output']) for c in child), 'child did not run failing command'
        assert any(b.get('type')=='tool_result' and b.get('is_error') for c in child for m in c['context']['messages'] for b in (m.get('content',[]) if isinstance(m.get('content'),list) else [])), 'child failure not present in real context'
    return {'case':case,'status':'passed','sample':str(sample),'capture_status':manifest['capture_status'],
            'requests':len(calls),'exported_calls':len(exported),'participants':len(parts),
            'root_tools':tools,'usage':info.get('usage')}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--api-base',required=True)
    parser.add_argument('--api-key-env',required=True)
    parser.add_argument('--model',default='claude-opus-5')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--cases',nargs='+',choices=list(CASES),default=list(CASES))
    parser.add_argument('--disable-experimental-betas',action='store_true',help='use Claude native compatibility setting for gateways rejecting beta request fields')
    args=parser.parse_args()
    if args.disable_experimental_betas:
        os.environ['CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS']='1'
    if not os.environ.get(args.api_key_env):
        parser.error('credential environment variable is empty')
    args.output.mkdir(parents=True,exist_ok=True)
    results=[]
    for case in args.cases:
        children,prompt=CASES[case]
        tasks=args.output/f'{case}.tasks.json'
        tasks.write_text(json.dumps([{'id':case,'harness':'claude-code','model':args.model,'prompt':prompt}]))
        command=['collect','--tasks',str(tasks),'--claude-api-base',args.api_base,
                 '--claude-api-key-env',args.api_key_env,'--claude-capture-calls',
                 '--claude-max-turns','6','--timeout','240','--parallel','1','--retries','0',
                 '--output',str(args.output),'--run-name',case]
        if args.disable_experimental_betas:
            command+=['--pass-env','CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS']
        try:
            exit_code=_collect(_build_parser().parse_args(command))
            assert exit_code==0, f'collect exit={exit_code}; inspect sample info.json'
            result=audit(args.output/case,children,case)
            before=(args.output/case/'calls.jsonl').read_bytes()
            assert _collect(_build_parser().parse_args(command+['--resume']))==0
            resumed=json.loads((args.output/case/'info.json').read_text())
            assert (args.output/case/'calls.jsonl').read_bytes()==before, 'resume changed calls export'
            result['resume_verified']=True
        except Exception as exc:
            result={'case':case,'status':'failed','error':str(exc)}
        results.append(result)
        (args.output/'summary.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
        print(json.dumps(result,ensure_ascii=False),flush=True)
    return 0 if all(r['status']=='passed' for r in results) else 1


if __name__=='__main__':
    raise SystemExit(main())
