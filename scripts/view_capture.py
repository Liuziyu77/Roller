"""Print a compact agent tree from an attempt directory (legacy or compact).

Usage: PYTHONPATH=src python scripts/view_capture.py /path/to/attempt-0001
"""
import argparse
import json
from pathlib import Path

from roller.capture import read_jsonl


def render(directory: Path) -> str:
    manifest = json.loads((directory / 'manifest.json').read_text())
    calls = read_jsonl(directory / 'calls.jsonl')
    relations = read_jsonl(directory / 'relations.jsonl')
    parts = {p['participant_id']: p for p in manifest['participants']}
    lines = [f"{manifest['harness']} / {manifest['attempt_id']} / {manifest['capture_status']}"]
    if manifest.get('lifecycle'):
        lifecycle = manifest['lifecycle']
        lines.append(f"收尾: {lifecycle['status']}；待完成: {len(lifecycle['pending_participant_ids'])}")
    seen = set()

    def visit(pid, indent=''):
        if pid in seen:
            lines.append(indent + f'↳ {pid} (already shown)')
            return
        seen.add(pid)
        part = parts[pid]
        lines.append(indent + ('主代理' if part['role'] == 'root' else '子代理') + f' [{pid}]')
        for call in calls:
            if call.get('participant_id') != pid:
                continue
            output = call.get('output') or {}
            blocks = output.get('content') or output.get('output') or []
            if call['protocol'] == 'openai-chat-completions':
                message = next(iter(output.get('choices', [])), {}).get('message', {})
                blocks = [{'type': 'text', 'text': message.get('content') or ''},
                          *[{'type': 'tool_use', 'name': t.get('function', {}).get('name', '')}
                            for t in message.get('tool_calls', [])]]
            summaries = []
            for block in blocks:
                if block.get('type') in {'tool_use', 'function_call'}:
                    summaries.append('调用 ' + block.get('name', ''))
                elif block.get('type') in {'text', 'output_text'}:
                    summaries.append(block.get('text', ''))
                elif block.get('type') == 'message':
                    summaries.extend(b.get('text', '') for b in block.get('content', [])
                                     if b.get('type') in {'text', 'output_text'})
            text = '；'.join(summaries).replace('\n', ' ')[:240]
            lines.append(indent + f"  调用 {call['request_seq']}: {text or call['execution_status']}")
            for relation in relations:
                if relation['type'] == 'spawn' and relation.get('parent_call_id') == call['call_id']:
                    child = relation.get('child_participant_id')
                    if child in parts:
                        visit(child, indent + '    ')
        kinds = {r['type'] for r in relations if r.get('child_participant_id') == pid}
        if part['role'] == 'child':
            lines.append(indent + '  结果已进入父代理上下文' if 'result_in_context' in kinds
                         else indent + '  尚未确认结果进入父代理上下文')

    for pid, part in parts.items():
        if part['role'] == 'root':
            visit(pid)
    for pid in parts:
        if pid not in seen:
            visit(pid)
    unknown = [c for c in calls if c.get('participant_id') is None]
    if unknown:
        lines.append(f'未归属调用: {len(unknown)}')
    return '\n'.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    print(render(parser.parse_args().directory))
