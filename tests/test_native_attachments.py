from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from roller.bundles import TaskBundleStore
from roller.harnesses.codex import CodexConfig, CodexHarness
from roller.harnesses.claude_code import ClaudeCodeConfig, ClaudeCodeHarness
from roller.harnesses.opencode import OpenCodeConfig, OpenCodeHarness
from roller.harnesses.pi import PiConfig, PiHarness
from roller.harnesses.deepseek import DeepSeekConfig, DeepSeekHarness
from roller.models import InputFile, RunRequest, Task
from roller.native_images import attachment_specs, attachment_source, claude_message, openclaw_message
from roller.tasks import load_tasks, TaskFormatError


class NativeAttachmentTests(unittest.TestCase):
    def request(self, root, *, selected=True):
        source = root / "image.png"
        source.write_bytes(b"image-test-bytes")
        task = Task("sample", ("Inspect",), 0, inputs=(InputFile(source, "inputs/image.png"),),
                    attach_images=("inputs/image.png",) if selected else ())
        return RunRequest(0, task, "model", 30)

    def test_no_implicit_attachment_and_first_turn_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = self.request(Path(tmp), selected=False)
            self.assertEqual(attachment_specs(request, "/workspace", first_turn=True), [])
            request = replace(request, task=replace(request.task, attach_images=("inputs/image.png",)))
            self.assertEqual(attachment_specs(request, "/workspace", first_turn=True)[0]["path"], "/workspace/inputs/image.png")
            self.assertEqual(attachment_specs(request, "/workspace", first_turn=False), [])

    def test_uses_materialized_bytes_with_no_original_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = self.request(root)
            bundle = TaskBundleStore(root / "run").prepare(request.task)
            request = replace(request, bundle=bundle, task=replace(request.task, inputs=(InputFile(None, "inputs/image.png"),)))
            self.assertEqual(attachment_source(request, "inputs/image.png").read_bytes(), b"image-test-bytes")
            message = claude_message(request, "Inspect", first_turn=True)
            self.assertEqual([b["type"] for b in message["message"]["content"]], ["text", "image"])

    def test_attachment_selection_changes_resume_digest_not_copy_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = self.request(root, selected=False)
            store = TaskBundleStore(root / "run")
            unselected = store.prepare(request.task)
            task = replace(request.task, attach_images=("inputs/image.png",))
            selected = store.prepare(task)
            self.assertNotEqual(unselected.manifest_digest, selected.manifest_digest)
            self.assertEqual(unselected.input_tree_digest, selected.input_tree_digest)
            copied = store.prepare(replace(task, input_mode="copy"))
            self.assertEqual(json.loads(copied.manifest_path.read_text())["files"]["input_mode"], "copy")

    def test_task_json_keeps_mount_copy_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.request(root)
            path = root / "tasks.json"
            row = {"id": "s", "prompt": "Inspect", "files": {"inputs": [{"source": "image.png", "target": "inputs/image.png"}],
                    "input_mode": "copy", "attach_images": ["inputs/image.png"]}}
            path.write_text(json.dumps([row]))
            task = load_tasks(path)[0]
            self.assertEqual(task.attach_images, ("inputs/image.png",))
            self.assertEqual(task.input_mode, "copy")
            row["files"]["attach_images"] = "inputs/image.png"
            path.write_text(json.dumps([row]))
            with self.assertRaises(TaskFormatError):
                load_tasks(path)

    def test_codex_initial_command_attaches_explicit_images_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = self.request(Path(tmp))
            harness = CodexHarness(CodexConfig(), 0, runtime=Mock())
            first = harness._codex_command("container", request, "Inspect", resume=False)
            later = harness._codex_command("container", request, "Again", resume=True)
            self.assertEqual(first[first.index("--image") + 1], "/roller/workspace/inputs/image.png")
            self.assertNotIn("--image", later)

    def test_claude_uses_native_stdin_and_removes_temporary_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = self.request(root)
            runtime = Mock()
            runtime.run.return_value.returncode = 0
            runtime.popen.return_value.wait.return_value = 0
            harness = ClaudeCodeHarness(ClaudeCodeConfig(), 0, runtime=runtime)
            captured = []
            def capture(args, *unused, **kwargs):
                if args[0] == "cp":
                    captured.append(json.loads(Path(args[1]).read_text()))
                return Mock(returncode=0)
            runtime.run.side_effect = capture
            harness._run_turn("container", request, "Inspect", resume=False, timeout_seconds=1, log_path=root / "rollout" / "trace.log")
            command = runtime.popen.call_args.args[0]
            self.assertIn("--input-format=stream-json", command)
            self.assertEqual(captured[0]["message"]["content"][1]["type"], "image")
            self.assertFalse((root / "rollout" / ".claude-input.jsonl").exists())

    def test_openclaw_markers_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = self.request(Path(tmp))
            self.assertIn("[media attached: /workspace/inputs/image.png", openclaw_message(request, "Inspect", "/workspace", first_turn=True))
            self.assertEqual(openclaw_message(request, "Again", "/workspace", first_turn=False), "Again")

    def test_opencode_reset_rearms_attachment_without_resetting_total_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = self.request(root)
            harness = OpenCodeHarness(OpenCodeConfig(vision=True), 0, runtime=Mock())
            session = harness.create_session(request, root / "run", 1)
            session.container_started = True
            session.turns = 2
            with patch.object(session, "checked"):
                session.reset()
            self.assertTrue(session._images_after_reset)
            self.assertEqual(session.turns, 2)

    def test_sdk_sessions_send_selected_images_only_on_initial_turn(self):
        for factory, config in [(PiHarness, PiConfig()), (DeepSeekHarness, DeepSeekConfig()),
                                (OpenCodeHarness, OpenCodeConfig(vision=True))]:
            with self.subTest(harness=factory.__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                request = self.request(root)
                runtime = Mock()
                runtime.popen.return_value.wait.return_value = 0
                harness = factory(config, 0, runtime=runtime)
                session = harness.create_session(request, root / "run", 1)
                session.container_started = True
                payloads = []
                def checked(args, *unused, **kwargs):
                    if args[0] == "cp":
                        payloads.append(json.loads(Path(args[1]).read_text()))
                    return Mock(returncode=0, stdout=json.dumps({"ok": True, "content": "done"}))
                with patch.object(session, "checked", side_effect=checked):
                    session.send("Inspect", 2)
                    session.send("Again", 2)
                    if factory is OpenCodeHarness:
                        session.reset()
                        session.send("New session", 2)
                key = "files" if factory is OpenCodeHarness else "images"
                self.assertEqual(len(payloads[0][key]), 1)
                self.assertEqual(payloads[1][key], [])
                if factory is OpenCodeHarness:
                    self.assertEqual(len(payloads[2][key]), 1)

    def test_unsafe_or_undeclared_paths_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = self.request(Path(tmp))
            for path in ("../escape.png", "/escape.png", "missing.png", "inputs/image.png\n"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    attachment_specs(replace(request, task=replace(request.task, attach_images=(path,))), "/workspace", first_turn=True)
