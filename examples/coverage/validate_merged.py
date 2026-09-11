#!/usr/bin/env python3
"""Validate the single merged JSON, including each negative case, without API calls."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import tempfile

from suite import ROOT, absolute_sources, check
from roller.bundles import TaskBundleStore
from roller.tasks import build_requests, load_tasks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tasks', type=Path, default=ROOT / 'all_tasks.json')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    path = args.tasks.resolve()
    merged = json.loads(path.read_text())
    tasks = load_tasks(path)
    coverage = merged['coverage']
    check(len(tasks) == len(coverage), 'task/coverage count mismatch')
    groups = Counter(item['case'] for item in coverage)
    check(all(count == 4 for count in groups.values()), 'each case must have four tasks')
    check(len({task.id for task in tasks}) == len(tasks), 'duplicate task IDs')
    negatives = []
    with tempfile.TemporaryDirectory(prefix='roller-merged-validation-') as temporary:
        root = Path(temporary)
        bundles = TaskBundleStore(root / 'positive').prepare_all(tasks)
        requests = build_requests(tasks, None, 180)
        check(len(requests) == len(tasks), 'request count mismatch')
        for task, item in zip(tasks, coverage):
            check(task.id == item['task_id'], 'coverage order mismatch')
            check(task.source_index == item['source_index'], 'source index mismatch')
            expected = item['expect']
            if 'resume_safe' in expected:
                check(bundles[task.id].resume_safe == expected['resume_safe'], 'context policy mismatch')
        negative_groups = Counter(item['case'] for item in merged['negative_cases'])
        check(all(count == 4 for count in negative_groups.values()), 'each negative group must have four tasks')
        for index, item in enumerate(merged['negative_cases']):
            task_path = root / 'negative.json'
            row = absolute_sources(item['task'], path.parent)
            task_path.write_text(json.dumps([row], ensure_ascii=False))
            try:
                rejected = load_tasks(task_path)
                TaskBundleStore(root / f'negative-{index}').prepare_all(rejected)
            except (ValueError, OSError) as exc:
                check(item['error_contains'] in str(exc), f'unexpected rejection: {exc}')
                negatives.append({'task_id': row.get('id'), 'case': item['case'], 'passed': True, 'error': str(exc)})
            else:
                raise ValueError(f'negative case accepted: {row.get("id")}')
    result = {'tasks': len(tasks), 'groups': len(groups), 'tasks_per_group': 4, 'negative_cases': len(negatives), 'by_harness': dict(Counter(task.harness for task in tasks)), 'simulator_tasks': sum(task.user_simulator is not None for task in tasks), 'passed': True, 'negative_results': negatives}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in result.items() if key != 'negative_results'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
