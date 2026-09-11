#!/usr/bin/env python3
"""Merge the runnable matrix and isolated negative cases into one JSON file."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1] / 'src'))
from roller.tasks import load_tasks
from suite import DEFAULT_MODELS, HARNESSES, normalized, select


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', help='shared model ID; per-Harness options take precedence')
    parser.add_argument('--openclaw-provider', default='my-openai-proxy')
    parser.add_argument('--case', action='append', help='case key or quoted glob; repeatable; default: all groups')
    parser.add_argument('--absolute-roots', action='store_true', help='materialize absolute-root cases for a local run; omit for portable templates')
    for harness in HARNESSES:
        parser.add_argument('--' + harness + '-model', help='override the model for this Harness, including Simulator participants')
    parser.add_argument('--output', type=Path, default=ROOT / 'all_tasks.json')
    args = parser.parse_args()
    output = args.output.resolve()
    models = {}
    for harness in HARNESSES:
        shared = f'{args.openclaw_provider}/{args.model}' if harness == 'openclaw' and args.model else args.model
        # No implicit environment binding: regeneration must stay deterministic.
        models[harness] = getattr(args, harness.replace('-', '_') + '_model') or shared or DEFAULT_MODELS[harness]
    if output == ROOT / 'all_tasks.json' and (models != DEFAULT_MODELS or args.case or args.absolute_roots):
        parser.error('use --output runs/coverage/<name>/tasks.json for local models, subsets, or absolute paths')
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    cases = select(manifest['cases'], args.case)
    rows, index = [], []
    for case in cases:
        tasks = normalized(case)
        for position, (task, expected) in enumerate(zip(tasks, case['expectations']), 1):
            task_id = case['key'].replace('/', '__') + f'__{position:02d}'
            row = copy.deepcopy(dict(task.raw))
            # A single file cannot have different CLI prompt/id keys or duplicate IDs.
            for field in ('prompt', 'question', 'messages', 'instruction', 'case_id', 'task_agent', 'agent', 'agent_harness'):
                row.pop(field, None)
            row.update(schema_version='roller.task/v1', id=task_id, harness=case['harness'])
            row['model'] = models[case['harness']]
            original_dir = (ROOT / case['path']).parent
            file_root = Path(task.raw.get('file_root', original_dir))
            if not file_root.is_absolute():
                file_root = original_dir / file_root
            row['file_root'] = str(file_root.resolve()) if args.absolute_roots and case.get('runtime_absolute_file_root') else os.path.relpath(file_root.resolve(), output.parent)
            if task.workspace is not None:
                row.pop('workspace_path', None)
                row['workspace'] = os.path.relpath(task.workspace, output.parent)
            if task.user_simulator:
                spec = row['user_simulator']
                if spec.get('model') is not None:
                    spec['model'] = models[spec['harness']]
            elif len(task.prompts) == 1:
                row['prompt'] = task.prompts[0]
            else:
                row['messages'] = [{'role': 'user', 'content': prompt} for prompt in task.prompts]
            expect = copy.deepcopy(expected)
            expect['task_id'] = task_id
            index.append({'task_id': task_id, 'source_index': len(rows), 'case': case['key'], 'original_id': task.id, 'original_path': os.path.relpath(ROOT / case['path'], output.parent), 'tier': case['tier'], 'expect': expect})
            rows.append(row)
    negatives = []
    for group in manifest['negative']:
        path = ROOT / group['path']
        for entry in json.loads(path.read_text()):
            item = copy.deepcopy(entry)
            root = (path.parent / item['task']['file_root']).resolve()
            item['task']['file_root'] = os.path.relpath(root, output.parent)
            item['case'] = group['key']
            negatives.append(item)
    result = {
        'schema_version': 'roller.examples-merged/v1',
        'models': models,
        'note': 'tasks contains runnable tasks; negative_cases must be checked individually. Per-file formats, custom CLI keys and IDs are normalized. Paths are portable unless --absolute-roots is used. Use suite.py for original-format coverage.',
        'tasks': rows,
        'negative_cases': negatives,
        'coverage': index,
        'environment': {'ROLLER_EXAMPLE_TOKEN': 'ENV_OK'},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    loaded = load_tasks(output)
    assert len(loaded) == sum(case['count'] for case in cases)
    assert len({task.id for task in loaded}) == len(loaded)
    print(f'{output}: {len(loaded)} runnable tasks + {len(negatives)} isolated negative cases')


if __name__ == '__main__':
    main()
