"""Self-authored tiny render; never executes a collected/historical script."""
import base64
import json
from pathlib import Path
import subprocess

from roller.sft import actf_to_sft
from roller_chart2code.acceptance import file_hash
from roller_chart2code.profiles import freeze_profile
from chart2code.png_fixture import png

ROOT = Path(__file__).resolve().parents[2]


def dump(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def image(data):
    return {"type": "image", "mimeType": "image/png", "data": base64.b64encode(data).decode()}


def step(index, *, user="", call=None, content="done"):
    calls = [] if call is None else [{"id": call, "name": "exec", "arguments": {}}]
    return {"step_id": index, "user_content": user,
            "assistant_content": {"content": "done" if call is None else "", "tool_calls": calls},
            "observation": [] if call is None else [{"toolCallId": call, "content": content, "isError": False}]}


def refresh(sample, actf=None, info=None):
    if actf is not None:
        dump(sample / "trace_ACTF.json", actf)
        dump(sample / "trace_SFT.json", actf_to_sft(actf))
    info = info or json.loads((sample / "info.json").read_text())
    outputs = sample / "results"
    info["output_collection"] = {"passed": True, "files": [
        {"path": p.relative_to(outputs).as_posix(), "size": p.stat().st_size, "sha256": file_hash(p)}
        for p in sorted(outputs.rglob("*")) if p.is_file()]}
    dump(sample / "info.json", info)
    return info


def fixture(sample, profile="single-render-v2", *, markers=False, output_view=False):
    sample.mkdir(parents=True, exist_ok=True)
    outputs = sample / "outputs"
    outputs.mkdir()
    script = outputs / "plot.py"
    script.write_text("from pathlib import Path\nPath(__file__).with_name('output.png').write_bytes(" + repr(png()) + ")\n")
    result = subprocess.run(["bash", str(ROOT / "docker/chart2code/python-with-snapshots.sh"), "outputs/plot.py"],
                            cwd=sample, capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise AssertionError(result.stderr)
    outputs.rename(sample / "results")
    # Different bytes from the output; still a real PNG (ancillary text chunk).
    import struct, zlib
    data = b"reference\0fixture"
    chunk = struct.pack(">I", len(data)) + b"tEXt" + data + struct.pack(">I", zlib.crc32(b"tEXt" + data))
    reference = png()[:-12] + chunk + png()[-12:]
    import hashlib
    identity = {"task_id": "tiny", "model": "synthetic", "harness": "fake", "request_index": 0,
                "sample_index": 0, "sample_count": 1, "source_index": 0}
    contract = {"profile": freeze_profile(profile, "inputs/reference.png"),
                "reference_sha256": hashlib.sha256(reference).hexdigest(), "identity": identity}
    # Markers are synthetic association test inputs, NOT emitted by the legacy recorder.
    rendered = "[roller-render attempt_000001 status=success]" if markers else result.stdout
    actf = {"schema_version": "ACTF_v1.0", "system_prompt": "", "tools": [
        {"type": "function", "name": "exec", "parameters": {"type": "object", "properties": {}}}],
        "steps": [step(1, user=[{"type": "text", "text": contract["profile"]["prompt"]}, image(reference)],
                       call="render", content=rendered)]}
    if output_view:
        actf["steps"].append(step(2, call="view", content=[image(png())]))
    actf["steps"].append(step(len(actf["steps"]) + 1))
    (sample / "trace.jsonl").write_text('{"type":"synthetic-fixture"}\n')
    info = {**identity, "schema_version": "roller.sample/v1", "status": "ok", "runtime": {},
            "usage": {"request_count": 1}, "training": {"eligible": True}, "conversion": {"status": "ok"},
            "trajectory": {"raw": "trace.jsonl", "actf": "trace_ACTF.json", "sft": "trace_SFT.json"}}
    refresh(sample, actf, info)
    return contract, actf, info
