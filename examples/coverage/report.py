#!/usr/bin/env python3
"""Evaluate a real merged-matrix run without issuing model requests."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1] / 'src'))
sys.path.insert(0, str(ROOT))
from suite import normalize_output, trace_requirements
from roller.integrity import validate_resumable_sample
from roller.models import RunRequest
from roller.tasks import load_tasks


def read(path):
    return json.loads(Path(path).read_text())


def redact(value):
    return re.sub(r'sk-[A-Za-z0-9_-]+', '<redacted>', str(value))


def evaluate(info_path, coverage, task, requirements):
    info = read(info_path)
    sample = info_path.parent
    expect = coverage['expect']
    row = {'task_id': info['task_id'], 'case': coverage['case'], 'harness': info['harness'], 'simulator': task.user_simulator.harness if task.user_simulator else None, 'status': info['status'], 'elapsed_seconds': info.get('timing', {}).get('elapsed_seconds'), 'usage': info.get('usage', {}), 'sample_path': str(sample.resolve()), 'failures': []}
    if info['status'] != 'ok':
        row['failures'].append(redact(info.get('error')))
    try:
        request = RunRequest(info['request_index'], task, info['model'], 180)
        validate_resumable_sample(sample, request, info, **requirements)
        actf, sft = read(sample / 'trace_ACTF.json'), read(sample / 'trace_SFT.json')
        user_turns = sum(m['role'] == 'user' for m in sft['messages'])
        row['user_turns'] = user_turns
        if user_turns != expect['user_turns']:
            row['failures'].append(f'user_turns expected {expect["user_turns"]}, got {user_turns}')
        assistants = [m for m in sft['messages'] if m['role'] == 'assistant']
        row['reasoning_chars'] = sum(len(m.get('reasoning_content', '')) for m in assistants)
        row['tool_calls'] = sum(len(m.get('tool_calls', [])) for m in assistants)
        calls = [call for message in assistants for call in message.get('tool_calls', [])]
        row['tool_names'] = sorted({call.get('function', {}).get('name', '') for call in calls})
        if coverage['tier'] == 'vision':
            viewed = False
            for call in calls:
                function = call.get('function', {})
                name = function.get('name', '').lower()
                arguments = str(function.get('arguments', ''))
                viewed |= ('image' in name or 'view_image' in arguments or (name == 'read' and '.png' in arguments))
            row['vision_tool_invoked'] = viewed
            decoded = any(re.search(r'\bzlib\b|\bPIL\b|Image\.open|getpixel', str(call.get('function', {}).get('arguments', ''))) for call in calls)
            row['pixel_decoding_fallback'] = decoded
            if decoded:
                row['failures'].append('used code to decode pixels despite the explicit visual-only instruction')
            if not viewed:
                row['failures'].append('no image-viewing tool call; pixel decoding is not a visual-understanding test')
        replies = [m.get('content', '') for m in assistants if m.get('content')]
        row['final_reply'] = replies[-1].strip() if replies else ''
        if 'reply' in expect and row['final_reply'] != expect['reply']:
            row['failures'].append(f'final_reply expected {expect["reply"]!r}, got {row["final_reply"]!r}')
        if row['tool_calls'] < expect.get('tool_calls_min', 0):
            row['failures'].append('expected a tool call')
        for name, expected in expect.get('files', {}).items():
            path = sample / 'results' / name
            if not path.is_file():
                row['failures'].append(f'missing output: {name}')
            else:
                actual = path.read_text().strip()
                normalized = normalize_output(coverage['case'], task, actual)
                if normalized != actual:
                    row.setdefault('output_normalizations', {})[name] = 'removed declared directory prefix; prompt did not specify relative-path base'
                if normalized != expected:
                    row['failures'].append(f'output {name}: expected {expected!r}, got {actual!r}')
        if expect.get('no_output_files') and any(p.is_file() for p in (sample / 'results').rglob('*')):
            row['failures'].append('unexpected output files')
        if 'resume_safe' in expect and info.get('provenance', {}).get('resume_safe') != expect['resume_safe']:
            row['failures'].append('wrong context resume policy')
        if 'rounds' in expect:
            conversation = read(sample / 'conversation.json')
            row['rounds'] = len(conversation['rounds'])
            row['termination'] = conversation['termination']['reason']
            if row['rounds'] != expect['rounds']:
                row['failures'].append(f'rounds expected {expect["rounds"]}, got {row["rounds"]}')
            if row['termination'] != expect['termination']:
                row['failures'].append(f'termination expected {expect["termination"]}, got {row["termination"]}')
            for role, harness in [('task_agent', task.harness), ('user_simulator', task.user_simulator.harness)]:
                if conversation['participants'][role]['harness'] != harness:
                    row['failures'].append(f'wrong {role} harness')
        if 'score' in expect and read(sample / 'score.json')['scores']['overall_score'] != expect['score']:
            row['failures'].append('wrong bench score')
        row['trajectory_integrity'] = True
    except (ValueError, OSError, KeyError, TypeError) as exc:
        row['trajectory_integrity'] = False
        row['failures'].append(redact(exc))
    row['passed'] = not row['failures']
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tasks', type=Path, required=True, help='the merged JSON used for this run, with bound model names')
    parser.add_argument('--fail-on-check', action='store_true')
    args = parser.parse_args()
    merged = read(args.tasks)
    coverage = {item['task_id']: item for item in merged['coverage']}
    tasks = {task.id: task for task in load_tasks(args.tasks)}
    run_info = read(args.run_dir / 'info.json')
    requirements = trace_requirements(run_info)
    rows = []
    for path in sorted((args.run_dir / 'samples').glob('*/*/info.json')):
        try:
            task_id = read(path)['task_id']
            rows.append(evaluate(path, coverage[task_id], tasks[task_id], requirements))
        except FileNotFoundError:
            # A concurrently resumed run may remove an old sample for rerun.
            continue
    rows.sort(key=lambda row: coverage[row['task_id']]['source_index'])
    groups = defaultdict(lambda: Counter())
    for row in rows:
        item = groups[row['harness']]
        item['total'] += 1
        item['collected_ok'] += row['status'] == 'ok'
        item['passed'] += row['passed']
        item['failed'] += not row['passed']
    aggregate_matches = None
    if run_info.get('state') == 'finished':
        try:
            expected_records = [read(Path(row['sample_path']) / 'trace_SFT.json') for row in rows if row['status'] == 'ok']
            actual_records = [json.loads(line) for line in (args.run_dir / 'sft.jsonl').read_text().splitlines() if line.strip()]
            aggregate_matches = actual_records == expected_records
        except (OSError, ValueError):
            aggregate_matches = False
    expected_total = run_info.get('configuration', {}).get('trajectory_count')
    complete = run_info.get('state') == 'finished' and len(rows) == expected_total
    report = {'complete': complete, 'expected_total': expected_total, 'run_dir': str(args.run_dir.resolve()), 'models': merged.get('models', {'shared': merged.get('api_model')}), 'total': len(rows), 'passed': sum(row['passed'] for row in rows), 'collected_ok': sum(row['status'] == 'ok' for row in rows), 'by_harness': dict(groups), 'sft_aggregate_matches': aggregate_matches, 'run_info': run_info, 'samples': rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(redact(json.dumps(report, ensure_ascii=False, indent=2)) + '\n')
    print(json.dumps({k: report[k] for k in ('total', 'passed', 'collected_ok', 'by_harness')}, ensure_ascii=False, indent=2))
    for row in rows:
        if not row['passed']:
            print(row['task_id'], '; '.join(row['failures'])[:600])

    return int(args.fail_on_check and (not complete or aggregate_matches is not True or any(not row['passed'] for row in rows)))


if __name__ == '__main__':
    raise SystemExit(main())
