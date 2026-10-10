"""Instrument the pinned native cache wrapper inside a disposable container.

The wrapper's base.sessionId belongs to this exact stream invocation, so
parallel agents never share a mutable 'current agent' identity.
"""
from pathlib import Path

needle = "return streamFn(model, context, options);\n\t\t};\n\t\treturn wrapped;"
replacement = ('rollerCaptureAppend("/tmp/roller-capture-stream.jsonl", JSON.stringify({...base, '
               'stage: "stream:context", messages: context.messages}) + "\\n"); '
               'return streamFn(model, context, {...options, headers: {...options?.headers, '
               '"x-roller-native-session-id": base.sessionId}});\n\t\t};\n\t\treturn wrapped;')
matched = [p for p in Path("/app/dist").glob("selection-*.js") if needle in p.read_text()]
if len(matched) != 1 or matched[0].read_text().count(needle) != 1:
    raise RuntimeError("Pinned OpenClaw cache stream capture hook is unavailable")
path = matched[0]
path.write_text('import {appendFileSync as rollerCaptureAppend} from "node:fs";\n' + path.read_text().replace(needle, replacement))

# Observe the native occurrence-aware ID resolver rather than reproducing its
# strict/strict9/provider rules in Roller.
needle = "const next = duplicateToolCallIdStyle ==="
files = [p for p in Path("/app/dist").glob("tool-call-id-*.js") if needle in p.read_text()]
if len(files) != 1:
    raise RuntimeError("Pinned OpenClaw tool ID capture hook unavailable")
path = files[0]
source = path.read_text()
marker = "\n\t\tconst pending = pendingByRawId.get(id);"
start = source.index(needle)
offset = source.index(marker, start)
source = source[:offset] + ('\n\t\trollerCaptureAppend("/tmp/roller-tool-id-aliases.jsonl", '
    'JSON.stringify({original_id: id, context_id: next}) + "\\n");') + source[offset:]
path.write_text('import {appendFileSync as rollerCaptureAppend} from "node:fs";\n' + source)
