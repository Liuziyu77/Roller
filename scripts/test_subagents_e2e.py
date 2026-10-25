"""Real Docker/service acceptance checks. Credentials come only from an env var.

Example: PYTHONPATH=src python scripts/test_subagents_e2e.py \
  --harness opencode --api-base URL --api-key-env OPENAI_API_KEY --output /tmp/cc-e2e
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from roller.harness_registry import HARNESS_NAMES
from roller.capture import native_directory, read_jsonl, validate_capture
from roller.cli import _build_parser, _collect
from roller.model_protocols import tool_call_ids
from roller.opencode_actf import native_messages


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


def harness_prompt(harness, case):
    children, prompt = CASES[case]
    if harness == "opencode":
        return children, prompt.replace('Agent','task').replace('general-purpose','general').replace('Explore','explore').replace('Bash','bash').replace('Write','write').replace('Read','read').replace('run_in_background=false','background=false')
    if harness == "claude-code" or case == "ordinary":
        return children, prompt
    if harness in {"pi", "deepseek"}:
        return None, None
    task = {
        "single": "Execute pwd once, then return CHILD_SINGLE.",
        "parallel": "Execute sleep 2 once, then return CHILD_PARALLEL.",
        "identical": "Return exactly IDENTICAL_CHILD. Do not use tools.",
        "workspace": "Create /roller/workspace/outputs/child.txt containing WORKSPACE_CHILD followed by a newline, then return CHILD_WROTE.",
        "tool-error": "Execute exit 13 once. Do not retry or repair the failure. Then return CHILD_ERROR_HANDLED.",
        "different-tools": "Execute pwd once, then return CONFIG_CHILD.",
    }[case]
    ending = "After receiving all completed child results, reply ROOT_" + case.replace('-', '_').upper() + ". "
    if case == "workspace":
        ending += "Read the file yourself to verify it after the child has written it. "
    if harness == "hermes":
        count = "TWO children in a single delegate_task tasks batch" if children == 2 else "ONE child using delegate_task"
        return children, f"Delegate {count}. Each child task: {task} " + ending + "Do not perform the child's work yourself. Use only delegate_task" + (" and read_file." if case == "workspace" else ".")
    if harness == "codex":
        profile = "Use the explorer agent type for the child. " if case == "different-tools" else ""
        return children, f"Use multi_agent_v1.spawn_agent to start {children} child agent(s). Each task: {task} " + profile + "Start all children before waiting; use multi_agent_v1.wait_agent to receive their final results. " + ending + "Do not perform the child's work yourself."
    task = task.replace("/roller/workspace/", "/tmp_workspace/")
    return children, f"Use sessions_spawn to start {children} child agent(s), with cleanup=keep and expectsCompletionMessage=true. Each task: {task} Start all children before waiting. Wait for completion notifications; you may use sessions_history to read the completed child result. " + ending + "Do not perform the child's work yourself."


def audit(run: Path, expected_children: int, case: str, harness="claude-code"):
    agent_tool = {"claude-code": "Agent", "opencode": "task", "hermes": "delegate_task", "codex": "spawn_agent", "openclaw": "sessions_spawn"}.get(harness)
    read_tool = {"claude-code": "Read", "opencode": "read", "hermes": "read_file", "codex": "exec_command", "openclaw": "read"}.get(harness)
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
    assert sum(p['role']=='root' for p in parts)==1, parts
    assert all(p['capture_status']=='complete' for p in parts), parts
    # Service-side failed attempts may remain raw/unknown. Every successful
    # model response must be completely captured and uniquely attributed.
    assert all(c['capture_status']=='complete' for c in calls if c['execution_status']=='completed'), [(c['request_seq'],c['errors']) for c in calls]
    assert all(c['export_status']=='unavailable' for c in calls if c['execution_status']!='completed')
    exported=read_jsonl(run/'calls.jsonl')
    assert len(exported)==sum(c['export_status']=='available' for c in calls)
    root_sft = 'available'
    if (sample/'trace_SFT.json').is_file():
        sft=json.loads((sample/'trace_SFT.json').read_text())
    else:
        conversion = info['runtime'].get('trajectory_conversion') or {}
        assert harness == 'codex' and 'tool_search_output' in conversion.get('error', ''), conversion
        trajectory = json.loads((sample/'trace_ACTF.json').read_text())
        assert trajectory['tools_capture']['status'] == 'unsupported'
        root_sft = 'unavailable_dynamic_tools'
        sft = {'messages': [{'tool_calls': step['assistant_content'].get('tool_calls', [])}
                           for step in trajectory['steps']]}
    tools=[t.get('function', t.get('custom', t))['name'] for m in sft['messages'] for t in m.get('tool_calls',[])]
    if harness in {"claude-code", "opencode", "hermes"}:
        assert set(tools)<=({agent_tool,read_tool} if case=='workspace' else {agent_tool}), tools
    assert all(m.get('content')!='No response requested.' for m in sft['messages'])
    if case == 'single' and harness in {'claude-code', 'opencode', 'hermes'}:
        assert any(m.get('role') == 'tool' and 'CHILD_SINGLE' in json.dumps(m.get('content'), ensure_ascii=False)
                   for m in sft['messages']), 'child result is missing from root SFT'
    relations=read_jsonl(directory/'relations.jsonl')
    assert sum(r['type']=='spawn' and r['status']=='observed' for r in relations)==expected_children
    assert sum(r['type']=='child_completed' for r in relations)==expected_children
    assert len({r['child_participant_id'] for r in relations if r['type']=='result_in_context'})==expected_children
    if case=='workspace':
        assert (sample/'results/child.txt').read_text()=='WORKSPACE_CHILD\n'
    if case in {'parallel','identical'}:
        spawns=[c for c in calls if c['output'] and len(tool_call_ids(c['protocol'],c['output']))==2]
        if harness in {'claude-code', 'opencode'}:
            assert spawns, 'model did not issue two delegation calls in one response'
        intervals=[]
        for p in children:
            cc=[c for c in calls if c['participant_id']==p['participant_id']]
            intervals.append((min(c['started_at'] for c in cc),max(c['finished_at'] for c in cc)))
        assert max(a for a,b in intervals)<min(b for a,b in intervals), 'child execution intervals did not overlap'
    if case=='different-tools':
        roots={p['participant_id'] for p in parts if p['role']=='root'}
        root=[c for c in calls if c['participant_id'] in roots]
        child=[c for c in calls if c['participant_id'] and c['participant_id'] not in roots]
        assert root[0]['context']['tools']!=child[0]['context']['tools']
    if case=='tool-error':
        children_ids={p['participant_id'] for p in children}
        child=[c for c in calls if c['participant_id'] in children_ids]
        assert any('exit 13' in json.dumps(c['output']) for c in child), 'child did not run failing command'
        if harness=='claude-code':
            failure_observed = any(b.get('type')=='tool_result' and b.get('is_error')
                for c in child for m in c['context']['messages']
                for b in (m.get('content',[]) if isinstance(m.get('content'),list) else []))
        elif harness=='opencode':
            # OpenCode emits '(no output)' into model context for exit 13;
            # the exit code is only in native tool metadata. Verify that fact
            # and its exact tool-result link, without inventing an error flag.
            if manifest.get('raw_retained') is False:
                # Lean capture retains the actual command/result link, but
                # discards native exit metadata. Do not invent an error flag.
                failed_ids = {t['id'] for c in child if c['output']
                    for choice in c['output'].get('choices', [])
                    for t in choice.get('message', {}).get('tool_calls', [])
                    if t.get('function', {}).get('name') == 'bash'
                    and json.loads(t['function']['arguments']).get('command', '').strip() == 'exit 13'}
                assert any('CHILD_ERROR_HANDLED' in json.dumps(c['output']) for c in child), 'child did not complete recovery'
            else:
                native = native_messages([row for p in native_directory(directory).glob('*.jsonl') for row in read_jsonl(p)])
                failed_ids = {p['callID'] for m in native for p in m['parts']
                    if p.get('type')=='tool' and p.get('tool')=='bash'
                    and p.get('state',{}).get('metadata',{}).get('exit')==13}
            failure_observed = bool(failed_ids) and any(m.get('role')=='tool' and m.get('tool_call_id') in failed_ids
                for c in child for m in c['context']['messages'])
        else:
            failure_observed = any('13' in json.dumps(m) for c in child for m in (c['context'].get('messages', []) if c['protocol'] != 'openai-responses' else c['context'].get('input', [])) if m.get('role') == 'tool' or m.get('type') in {'function_call_output', 'custom_tool_call_output'})
        assert failure_observed, 'child tool failure lacks native/context evidence'

    return {'case':case,'status':'passed','sample':str(sample),'capture_status':manifest['capture_status'],
            'requests':len(calls),'exported_calls':len(exported),'participants':len(parts),
            'root_tools':tools,'root_sft':root_sft,'usage':info.get('usage')}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--capture-debug', choices=['off', 'on-error', 'full'], default='on-error')
    parser.add_argument('--api-base',required=True)
    parser.add_argument('--api-key-env',required=True)
    parser.add_argument('--model', help='default: gpt-5.5 for Codex/OpenClaw, claude-opus-5 otherwise')
    parser.add_argument('--harness',choices=HARNESS_NAMES,default='claude-code')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--cases',nargs='+',choices=list(CASES),default=list(CASES))
    parser.add_argument('--max-output-tokens', type=int, default=4096, help='OpenClaw model output budget')
    parser.add_argument('--thinking', help='explicit reasoning effort for this test run')
    parser.add_argument('--openclaw-api', choices=['openai-completions', 'openai-responses'], default='openai-responses')
    parser.add_argument('--gateway-port', type=int, help='first OpenClaw gateway port (default: a free range picked by Roller)')
    parser.add_argument('--check-followup', action='store_true', help='add a second user turn after delegation to verify native root-thread resume')
    parser.add_argument('--disable-experimental-betas',action='store_true',help='use Claude native compatibility setting for gateways rejecting beta request fields')
    args=parser.parse_args()
    if args.disable_experimental_betas:
        os.environ['CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS']='1'
    if not os.environ.get(args.api_key_env):
        parser.error('credential environment variable is empty')
    args.model = args.model or ('gpt-5.5' if args.harness in {'codex', 'openclaw'} else 'claude-opus-5')
    args.output.mkdir(parents=True,exist_ok=True)
    results=[]
    quota_exhausted = False
    for case in args.cases:
        if quota_exhausted:
            results.append({"case": case, "status": "blocked", "reason": "service token quota exhausted"})
            (args.output/"summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
            continue
        children, prompt = harness_prompt(args.harness, case)
        if children is None:
            result = {'case': case, 'status': 'not_applicable', 'reason': 'bundled profile has no native delegation tool or extensions'}
            results.append(result)
            (args.output/'summary.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
            print(json.dumps(result,ensure_ascii=False),flush=True)
            continue
        tasks=args.output/f'{case}.tasks.json'
        task = {'id':case,'harness':args.harness,'model':('roller/'+args.model if args.harness=='openclaw' else args.model)}
        if args.check_followup:
            task['messages'] = [prompt, 'Do not delegate or use tools again. Reply exactly ROOT_FOLLOWUP.']
        else:
            task['prompt'] = prompt
        tasks.write_text(json.dumps([task]))
        command=['collect','--tasks',str(tasks),'--api-base',args.api_base,
                 '--api-key-env',args.api_key_env,'--capture-calls','--capture-debug',args.capture_debug,
                 '--timeout','240','--parallel','1','--retries','0',
                 '--output',str(args.output),'--run-name',case]
        controls = {'claude-code': ['--claude-max-turns', '8'], 'opencode': ['--opencode-max-steps', '8'],
                    'hermes': ['--hermes-max-iterations', '8'], 'pi': ['--pi-max-steps', '8'],
                    'deepseek': ['--deepseek-max-steps', '8']}
        command += controls.get(args.harness, [])
        if args.thinking:
            command += ['--thinking', args.thinking]
        if args.harness == 'openclaw':
            config = {'providers': {'roller': {'baseUrl': args.api_base, 'apiKey': '${'+args.api_key_env+'}',
                'api': args.openclaw_api, 'models': [{'id': args.model, 'name': args.model,
                'input': ['text'], 'reasoning': True, 'contextWindow': 128000, 'maxTokens': args.max_output_tokens}]}}}
            config_path = args.output / 'model-config.json'
            config_path.write_text(json.dumps(config))
            command += ['--model-config', str(config_path), '--pass-env', args.api_key_env, '--openclaw-max-tokens', str(args.max_output_tokens)]
            if args.gateway_port is not None:
                command += ['--gateway-port', str(args.gateway_port)]

        if args.disable_experimental_betas:
            command+=['--pass-env','CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS']
        try:
            exit_code=_collect(_build_parser().parse_args(command))
            if exit_code != 0:
                sample_info = next((args.output/case/'samples').glob('*/*/info.json'), None)
                detail = json.loads(sample_info.read_text()).get('error', '') if sample_info else ''
                quota_exhausted = 'quota' in detail.lower() and '403' in detail
                raise AssertionError(f'collect exit={exit_code}: {detail}')
            result=audit(args.output/case,children,case,args.harness)
            if args.check_followup:
                sample = Path(result['sample'])
                info = json.loads((sample/'info.json').read_text())
                directory = (sample/info['runtime']['capture']['manifest']).parent
                parts = json.loads((directory/'manifest.json').read_text())['participants']
                root = next(p['participant_id'] for p in parts if p['role']=='root')
                calls = [c for c in read_jsonl(directory/'calls.jsonl') if c['participant_id']==root]
                assert 'ROOT_FOLLOWUP' in json.dumps(calls[-1]['output']), 'second turn did not finish in root thread'
                result['followup_verified'] = True
            before=(args.output/case/'calls.jsonl').read_bytes()
            assert _collect(_build_parser().parse_args(command+['--resume']))==0
            assert (args.output/case/'calls.jsonl').read_bytes()==before, 'resume changed calls export'
            result['resume_verified']=True
        except Exception as exc:
            result={'case':case,'status':'failed','error':str(exc)}
        results.append(result)
        (args.output/'summary.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
        print(json.dumps(result,ensure_ascii=False),flush=True)
    return 0 if all(r['status'] in {'passed', 'not_applicable'} for r in results) else 1


if __name__=='__main__':
    raise SystemExit(main())
