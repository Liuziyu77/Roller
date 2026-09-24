from __future__ import annotations

import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from roller.codex_actf import codex_events_to_actf
from roller.reexport_codex import reexport_run
from roller.sft import SFTConversionError, actf_to_sft, validate_sft_record


PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII="


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _sample(run: Path, index: int = 0, *, output=None) -> dict:
    """Build a legacy export with repeated images in separate model turns."""
    image = {"type": "input_image", "image_url": PNG, "detail": "original"}
    if output is None:
        output = [{"type": "input_text", "text": "frame"}, image]
    events = [{"type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "Inspect the picture twice."}],
    }}]
    for n, content in enumerate([output, [image, image]]):
        events.extend([
            {"type": "response_item", "payload": {
                "type": "reasoning", "summary": [{"text": "Private reasoning omitted from saved ACTF."}],
            }},
            {"type": "response_item", "payload": {
                "type": "function_call" if n == 0 else "custom_tool_call",
                "call_id": f"call-{n}", "name": "inspect_asset",
                "arguments": '{"path":"picture.png"}', "input": '{"path":"picture.png"}',
            }},
            {"type": "response_item", "payload": {
                "type": "function_call_output" if n == 0 else "custom_tool_call_output",
                "call_id": f"call-{n}", "output": content,
            }},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 12, "output_tokens": 3},
            }}},
        ])
    original = codex_events_to_actf(events, keep_thinking=False)
    original["system_prompt"] = "Captured system instructions."
    original["tools"][0]["function"]["description"] = "Captured tool schema."
    for step, content in zip(original["steps"], [output, [image, image]]):
        step["observation"][0]["content"] = [{"type": "text", "text": json.dumps(content)}]
    relative = f"samples/model/{index:08d}_image"
    sample = run / relative
    _write(sample / "info.json", {"harness": "codex", "status": "ok"})
    _write(sample / "trace_ACTF.json", original)
    _write(sample / "trace_SFT.json", actf_to_sft(original))
    (sample / "trace.jsonl").write_text("\n".join(map(json.dumps, events)) + "\n", encoding="utf-8")
    return {"request_index": index, "path": relative, "status": "ok", "task_id": "image", "model": "model"}


class ReexportCodexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / "source"
        self.entry = _sample(self.run)
        _write(self.run / "info.json", {"samples": [self.entry]})
        self.sample = self.run / self.entry["path"]
        self.output = self.root / "repaired"

    def test_reexports_images_without_changing_conversation_or_source(self):
        before = {p.relative_to(self.run): p.read_bytes() for p in self.run.rglob("*") if p.is_file()}
        original = json.loads((self.sample / "trace_ACTF.json").read_text())
        report = reexport_run(self.run, self.output)
        after = {p.relative_to(self.run): p.read_bytes() for p in self.run.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        result = self.output / self.entry["path"]
        repaired = json.loads((result / "trace_ACTF.json").read_text())
        record = json.loads((result / "trace_SFT.json").read_text())
        validate_sft_record(record)
        self.assertEqual(record["images"], [PNG, PNG, PNG])
        self.assertEqual(base64.b64decode(record["images"][0].split(",", 1)[1]), base64.b64decode(PNG.split(",", 1)[1]))
        contents = [m["content"] for m in record["messages"] if m["role"] == "tool"]
        self.assertEqual(contents, ["frame\n<image>", "<image>\n<image>"])
        self.assertNotIn("base64", json.dumps(record["messages"]))
        self.assertEqual(repaired["steps"][0]["observation"][0]["content"][1]["detail"], "original")
        # All non-observation fields, including intentionally stripped reasoning,
        # must remain identical to the historical export.
        for old, new in zip(original["steps"], repaired["steps"]):
            new["observation"] = old["observation"]
        self.assertEqual(repaired, original)
        self.assertEqual(json.loads((self.output / "sft.jsonl").read_text()), record)
        self.assertEqual(report["samples"][0]["image_count"], 3)
        self.assertEqual(json.loads((self.output / "reexport.json").read_text()), report)
        self.assertFalse((self.output / "info.json").exists())

    def test_orders_aggregate_and_records_skipped_samples(self):
        second = _sample(self.run, 2)
        second["status"] = "skipped"
        _write(self.run / "info.json", {"samples": [second, {"status": "error"}, self.entry]})
        report = reexport_run(self.run, self.output)
        self.assertEqual([s["request_index"] for s in report["samples"]], [0, 2])
        self.assertEqual([s["line_number"] for s in report["samples"]], [1, 2])
        self.assertEqual(report["skipped_samples"], 1)
        self.assertEqual(len((self.output / "sft.jsonl").read_text().splitlines()), 2)

    def test_invalid_image_aborts_batch_without_publishing_partial_export(self):
        second = _sample(self.run, 1, output=[{"type": "input_image", "image_url": "data:image/png;base64,not*base64"}])
        _write(self.run / "info.json", {"samples": [self.entry, second]})
        with self.assertRaises(SFTConversionError):
            reexport_run(self.run, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".codex-reexport-*")), [])

    def test_rejects_non_codex_and_mismatched_native_trace(self):
        _write(self.sample / "info.json", {"harness": "openclaw"})
        with self.assertRaisesRegex(ValueError, "only Codex"):
            reexport_run(self.run, self.output)
        _write(self.sample / "info.json", {"harness": "codex"})
        events = [json.loads(line) for line in (self.sample / "trace.jsonl").read_text().splitlines()]
        events = [e for e in events if e["payload"].get("call_id") != "call-1"]
        (self.sample / "trace.jsonl").write_text("\n".join(map(json.dumps, events)))
        with self.assertRaisesRegex(ValueError, "tool-call IDs differ"):
            reexport_run(self.run, self.output)
        self.assertFalse(self.output.exists())

    def test_rejects_existing_output_and_escaping_sample_path(self):
        with self.assertRaisesRegex(ValueError, "outside the source run"):
            reexport_run(self.run, self.run / "new-export")
        self.assertFalse((self.run / "new-export").exists())
        self.output.mkdir()
        sentinel = self.output / "keep.txt"
        sentinel.write_text("keep")
        with self.assertRaises(FileExistsError):
            reexport_run(self.run, self.output)
        self.assertEqual(sentinel.read_text(), "keep")
        for path in ["../outside", "/absolute", "samples/../../outside"]:
            with self.subTest(path=path):
                entry = {**self.entry, "path": path}
                _write(self.run / "info.json", {"samples": [entry]})
                with self.assertRaisesRegex(ValueError, "invalid sample path"):
                    reexport_run(self.run, self.root / "other")
                self.assertFalse((self.root / "other").exists())

    def test_cli_exports_offline(self):
        result = subprocess.run(
            [sys.executable, "-m", "roller.reexport_codex", str(self.run),
             "--output", str(self.output)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Re-exported 1 samples", result.stdout)
        self.assertEqual(len(json.loads((self.output / "sft.jsonl").read_text())["images"]), 3)


if __name__ == "__main__":
    unittest.main()
