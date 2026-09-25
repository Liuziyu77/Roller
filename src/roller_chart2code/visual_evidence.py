"""Separate wire image evidence from the main agent's observed conversation."""
import base64
import binascii
import hashlib
import json
from pathlib import Path


def image_hashes(value):
    if isinstance(value, dict):
        if value.get('type') in ('input_image', 'image_url', 'image'):
            url = value.get('image_url', value.get('url'))
            if isinstance(url, dict):
                url = url.get('url')
            if isinstance(url, str) and url.startswith('data:image/') and ';base64,' in url and len(url) <= 70_000_000:
                try:
                    yield hashlib.sha256(base64.b64decode(url.split(',', 1)[1], validate=True)).hexdigest()
                except (ValueError, binascii.Error):
                    pass
        for item in value.values():
            yield from image_hashes(item)
    elif isinstance(value, list):
        for item in value:
            yield from image_hashes(item)


def collect_visual_evidence(wire_path: Path, trajectory: dict, references: dict):
    """Link only unique tool calls by exact prompt and reference hash.

    Never inserts images into ACTF user/tool content. Ambiguous matches remain
    unlinked. Full request bodies remain in the original wire log.
    """
    calls = []
    for step in trajectory.get('steps', []):
        for call in step.get('assistant_content', {}).get('tool_calls', []):
            args = call.get('arguments', {})
            if call.get('name') != 'image' or not isinstance(args, dict):
                continue
            source = args.get('image')
            reference_hash = references.get(source)
            if reference_hash is None and isinstance(source, str):
                reference_hash = references.get(Path(source).name)
            if not reference_hash or not isinstance(args.get('prompt'), str):
                continue
            results = [r for r in step.get('observation', []) if r.get('toolCallId') == call.get('id')]
            if not results or any(r.get('isError') for r in results):
                continue
            calls.append((call, step, reference_hash, results))
    records = []
    if wire_path.is_file():
        with wire_path.open() as stream:
            for line_number, line in enumerate(stream, 1):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                body = event.get('body', {})
                if not isinstance(body, dict):
                    continue
                content = body.get('input', body.get('messages', []))
                hashes = list(image_hashes(content))
                if not hashes:
                    continue
                def texts(v):
                    if isinstance(v, dict):
                        if isinstance(v.get('text'), str):
                            yield v['text']
                        for x in v.values():
                            yield from texts(x)
                    elif isinstance(v, list):
                        for x in v:
                            yield from texts(x)
                text_blocks = list(texts(content))
                matches = [(c, s, h, r) for c, s, h, r in calls
                           if h in hashes and c['arguments']['prompt'] in text_blocks]
                record = dict(wire_line=line_number, request_timestamp=event.get('ts'),
                              model=body.get('model', event.get('model')), image_sha256=hashes,
                              parameters={k:body[k] for k in ('reasoning', 'reasoning_effort', 'max_tokens', 'max_output_tokens') if k in body},
                              parent_tool_call_id=None)
                if len(matches) == 1:
                    call, step, reference_hash, results = matches[0]
                    record.update(parent_tool_call_id=call['id'], reference_sha256=reference_hash,
                                  association='unique exact tool prompt + image hash + successful parent result',
                                  tool_result=results, direct_main_agent_image=False)
                records.append(record)
    return dict(schema='roller.visual_evidence/v1', records=records,
                linked_reference_requests=sum(bool(r['parent_tool_call_id']) for r in records))
