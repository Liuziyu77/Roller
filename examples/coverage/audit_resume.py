#!/usr/bin/env python3
"""Read-only audit of the collector's resume decision; never invokes a model."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
import io
import json
import logging
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1] / 'src'))
from roller.bundles import TaskBundleStore
from roller.collector import CollectionConfig, Collector
from roller.models import RunRequest
from roller.tasks import load_tasks
from suite import trace_requirements


def forbidden_factory(*args):
    raise AssertionError('This audit must never start a Harness or call an API.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tasks', type=Path, required=True, help='the merged task JSON used for this run')
    args = parser.parse_args()
    tasks = load_tasks(args.tasks)
    requirements = trace_requirements(json.loads((args.run_dir / 'info.json').read_text()))
    collector = Collector(CollectionConfig(
        run_dir=args.run_dir, resume=True, keep_aux_traces=requirements['require_wire_trace'],
        capture_cache_trace=requirements['require_cache_trace'],
    ), forbidden_factory)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger('roller.collector')
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    rows = []
    with tempfile.TemporaryDirectory(prefix='roller-coverage-resume-audit-') as temporary:
        bundles = TaskBundleStore(Path(temporary))
        prepared = bundles.prepare_all(tasks)
        requests = bundles.attach([
            RunRequest(task.source_index, task, task.model, 180) for task in tasks
        ], prepared)
        for request in requests:
            path = collector._sample_dir(request)
            if not (path / 'info.json').is_file():
                continue
            info = json.loads((path / 'info.json').read_text())
            request = replace(request, index=info['request_index'])
            stream.seek(0)
            stream.truncate(0)
            decision = collector._load_resumable(path, request)
            rows.append({
                'task_id': request.task.id,
                'harness': request.task.harness,
                'simulator': request.task.user_simulator.harness if request.task.user_simulator else None,
                'sample_status': info['status'],
                'context_resume_safe': request.bundle.resume_safe,
                'reusable': decision is not None,
                'reason': stream.getvalue().strip(),
            })
        bundles.cleanup_runtime()
    logger.removeHandler(handler)
    summary = {
        'audited': len(rows),
        'reusable': sum(row['reusable'] for row in rows),
        'context_none': sum(not row['context_resume_safe'] for row in rows),
        'rejected_missing_cache_trace': sum('cache_trace.jsonl is missing' in row['reason'] for row in rows),
        'sample_statuses': dict(Counter(row['sample_status'] for row in rows)),
    }
    result = {'method': 'Collector._load_resumable with freshly prepared bundles and the recorded trace policy. Checks sample integrity; does not validate the run lock or execute a Harness.', 'summary': summary, 'samples': rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
