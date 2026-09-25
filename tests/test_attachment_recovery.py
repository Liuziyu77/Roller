"""Offline journal recovery; fake bytes test transport, not image decoding."""
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from roller.attachment_journal import journal_image
from roller.deepseek_actf import deepseek_events_to_actf, extract_deepseek_usage
from roller.harnesses.opencode import RUNNER_PATH
from roller.opencode_actf import opencode_events_to_actf
from roller.sft import actf_to_sft, SFTConversionError
from test_deepseek import native, sample_events as deepseek_sample
from test_opencode import sample_events as opencode_sample

PAYLOAD = "AA=="
URI = "data:image/png;base64," + PAYLOAD
REF = {"attachmentId": "image-1", "mediaType": "image/png"}


def image_event(**extra):
    return {"type": "attachment", "attachment": dict(REF), "mimeType": "image/png", "data": PAYLOAD, **extra}


class DeepSeekAttachmentTests(unittest.TestCase):
    def events(self):
        events = deepseek_sample()
        blocks = [{"type": "text", "text": "before"},
                  {"type": "image", "attachment": dict(REF)},
                  {"type": "text", "text": "after\u2028next"}]
        events[2]["payload"]["event"]["data"]["content"] = deepcopy(blocks)
        events[4]["payload"]["event"]["data"]["message"]["content"][0]["content"] = deepcopy(blocks)
        return [{"type": "metadata", "session_id": "s1"}, image_event(), *events]

    def test_user_and_tool_images_preserve_order_without_mutating_native(self):
        events = self.events()
        before = deepcopy(events)
        actf = deepseek_events_to_actf(events)
        self.assertEqual(events, before)
        sft = actf_to_sft(actf)
        self.assertEqual(sft["images"], [URI, URI])
        for message in (sft["messages"][1], sft["messages"][3]):
            self.assertEqual(message["content"], "before\n<image>\nafter\u2028next")

    def test_missing_corrupt_changed_hash_or_foreign_bytes_rejected(self):
        for case in ("missing", "base64", "mime", "duplicate", "hash", "foreign"):
            with self.subTest(case=case):
                events = self.events()
                if case == "missing": events.pop(1)
                elif case == "base64": events[1]["data"] = "!"
                elif case == "mime": events[1]["mimeType"] = "image/jpeg"
                elif case == "duplicate": events.append(image_event(data="AQ=="))
                elif case == "hash": events[1]["sha256"] = "0" * 64
                elif case == "foreign": events[1]["session_id"] = "other"
                with self.assertRaises(ValueError): deepseek_events_to_actf(events)

    def test_other_session_notifications_and_usage_are_filtered(self):
        events = self.events()
        foreign = native("assistant/message", {"message": {"content": [{"type": "text", "text": "foreign"}]},
                                                "usage": {"inputTokens": 9999}}, 99)
        foreign["payload"]["sessionId"] = "other"
        events.append(foreign)
        self.assertNotIn("foreign", json.dumps(deepseek_events_to_actf(events)))
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "trace.jsonl"
            source.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in events), encoding="utf-8")
            self.assertEqual(extract_deepseek_usage(source)["input_tokens"], 30)
        events.pop(0)
        with self.assertRaisesRegex(ValueError, "metadata"):
            deepseek_events_to_actf(events)

    def test_multiple_target_sessions_not_silently_combined(self):
        with self.assertRaisesRegex(ValueError, "multiple target"):
            deepseek_events_to_actf(self.events() + [{"type": "metadata", "session_id": "other"}])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to test the native journal hook")
    def test_javascript_save_hooks_persist_bytes_and_restore_on_dispose(self):
        module = RUNNER_PATH.parent.parent / "deepseek" / "journal.mjs"
        script = r'''
import { pathToFileURL } from 'node:url';
import { readFileSync } from 'node:fs';
const plugin = await import(pathToFileURL(process.argv[1]));
let reads = 0, dispose;
const ref = {attachmentId: 'a1', mediaType: 'image/png'};
const attachments = {
  async saveImage() { return ref; }, async saveImages() { return [ref, ref]; },
  async readImage() { reads++; return {ref, data: Buffer.from([0])}; },
};
const original = attachments.saveImage;
const originalFetch = globalThis.fetch;
plugin.apply({attachments, subprocess: {spawnTerminal() {}}, on(name, fn) { dispose = fn; }});
await Promise.all([attachments.saveImage({}), attachments.saveImages([])]);
const rows = readFileSync(process.env.ROLLER_DEEPSEEK_JOURNAL, 'utf8').trim().split('\n').map(JSON.parse);
dispose();
if (reads !== 1 || rows.length !== 1 || rows[0].data !== 'AA==' || !rows[0].sha256 ||
    attachments.saveImage !== original || globalThis.fetch !== originalFetch) throw Error('bad journal hooks');
'''
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "ROLLER_DEEPSEEK_JOURNAL": str(Path(tmp) / "trace.jsonl")}
            result = subprocess.run(["node", "--input-type=module", "-e", script, str(module)],
                                    env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)


class OpenCodeAttachmentTests(unittest.TestCase):
    def runner(self):
        spec = importlib.util.spec_from_file_location("recovery_runner", RUNNER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def events(self, url):
        events = opencode_sample()
        messages = events[-1]["data"]["messages"]
        messages[0]["parts"] = [
            {"id": "p1a", "messageID": "m1", "type": "text", "text": "before"},
            {"id": "p1b", "messageID": "m1", "type": "file", "mime": "image/png", "url": url},
            {"id": "p1c", "messageID": "m1", "type": "text", "text": "after"}]
        messages[1]["parts"][1]["state"]["attachments"] = [{"mime": "image/png", "url": url}]
        return events

    def test_container_file_removed_before_offline_projection(self):
        runner = self.runner()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "image with spaces.png"
            path.write_bytes(b"\0")
            events = self.events(path.as_uri())
            records = list(runner.attachment_records(events[-1]["data"]))
            self.assertEqual(len(records), 2)
            self.assertTrue(all(row["sha256"] == hashlib.sha256(b"\0").hexdigest() for row in records))
            events.extend(records)
        # The source directory no longer exists: only journal bytes are used.
        sft = actf_to_sft(opencode_events_to_actf(events))
        self.assertEqual(sft["images"], [URI, URI])
        self.assertEqual(sft["messages"][1]["content"], "before\n<image>\nafter")

    def test_inline_payload_not_duplicated_and_image_only_user_is_preserved(self):
        events = self.events(URI)
        user = events[-1]["data"]["messages"][0]
        user["parts"] = [user["parts"][1]]
        self.assertEqual(list(self.runner().attachment_records(events[-1]["data"])), [])
        sft = actf_to_sft(opencode_events_to_actf(events))
        self.assertEqual(sft["messages"][1]["content"], "<image>")
        self.assertEqual(sft["images"], [URI, URI])

    def test_missing_bytes_remain_diagnostic_and_cannot_be_sft(self):
        events = self.events("file:///no-container/image.png")
        actf = opencode_events_to_actf(events)
        with self.assertRaises(SFTConversionError): actf_to_sft(actf)

    def test_changed_payload_and_wrong_hash_rejected(self):
        record = {"type": "attachment", "key": "part:p1b", "mime": "image/png", "data": PAYLOAD}
        for bad in ({**record, "data": "AQ=="}, {**record, "sha256": "0" * 64}):
            with self.assertRaises(ValueError):
                opencode_events_to_actf(self.events("file:///gone/image.png") + [record, bad])

    def test_runner_rejects_remote_unsafe_and_oversized_files(self):
        runner = self.runner()
        for url in ("https://example.invalid/image.png", "file://remote/tmp/image.png",
                    "file:///tmp/image%0A.png", "file:relative.png", "relative.png"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                runner._attachment_bytes({"mime": "image/png", "url": url})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oversize.png"
            path.write_bytes(b"00000")
            with patch.object(runner, "MAX_IMAGE_BYTES", 4), self.assertRaises(ValueError):
                runner._attachment_bytes({"mime": "image/png", "url": str(path)})


class JournalValidationTests(unittest.TestCase):
    def test_hash_and_bounded_base64(self):
        self.assertEqual(journal_image(PAYLOAD, "image/png", sha256=hashlib.sha256(b"\0").hexdigest())["data"], PAYLOAD)
        for data, mime in (("", "image/png"), ("!", "image/png"), (PAYLOAD, "text/plain")):
            with self.assertRaises(ValueError): journal_image(data, mime)
        with patch("roller.attachment_journal.MAX_IMAGE_BYTES", 1), self.assertRaises(ValueError):
            journal_image("AAAA", "image/png")
