"""Explicit tool-ID collision repair; preserve native IDs in an audit mapping."""
from copy import deepcopy
import uuid

class ToolIdRegistry:
    def __init__(self):
        self.seen = set()

    def response(self, event, mapping):
        event = deepcopy(event)
        if not isinstance(event, dict):
            return event
        blocks = []
        if event.get('type') == 'content_block_start':
            blocks = [event.get('content_block', {})]
        elif event.get('type') == 'message':
            blocks = event.get('content', [])
        for block in blocks:
            if not isinstance(block, dict) or block.get('type') != 'tool_use':
                continue
            native = block.get('id')
            if not isinstance(native, str) or not native:
                continue
            if native in self.seen:
                unique = 'toolu_roller_' + uuid.uuid4().hex
                block['id'] = unique
                mapping.append({'native_id': native, 'delivered_id': unique})
            self.seen.add(block['id'])
        return event


def scope_claude_events(rows):
    """Recover sequential reuse; reject concurrent reuse with ambiguous results.

    Repeated fragments of one assistant message are the same call. Generated
    identities never shadow a native ID anywhere in the input.
    """
    rows = deepcopy(rows)
    reserved = set()
    for row in rows:
        message = row.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        reserved.update(b["id"] for b in message["content"]
                        if isinstance(b, dict) and b.get("type") == "tool_use"
                        and isinstance(b.get("id"), str))
    used, calls, pending = set(), {}, {}
    for index, row in enumerate(rows):
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        identity = message.get("id") or row.get("uuid") or ("event", index)
        for block in content:
            if not isinstance(block, dict):
                continue
            if row.get("type") == "assistant" and block.get("type") == "tool_use":
                native = block.get("id")
                if not isinstance(native, str) or not native:
                    continue
                key = (identity, native)
                if key not in calls:
                    if native in pending:
                        raise ValueError("ambiguous overlapping Claude tool ID: " + native)
                    unique = native
                    count = 2
                    while unique in used or (unique != native and unique in reserved):
                        unique = f"{native}__occurrence_{count}"
                        count += 1
                    calls[key] = unique
                    used.add(unique)
                    pending[native] = unique
                unique = calls[key]
                if unique != native:
                    block["roller_original_tool_id"] = native
                block["id"] = unique
            elif row.get("type") == "user" and block.get("type") == "tool_result":
                native = block.get("tool_use_id")
                unique = pending.pop(native, None)
                if unique is not None:
                    if unique != native:
                        block["roller_original_tool_id"] = native
                    block["tool_use_id"] = unique
    return rows
