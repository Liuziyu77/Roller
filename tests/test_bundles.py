from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from roller.bundles import BundleError, TaskBundleStore
from roller.tasks import TaskFormatError, load_tasks


class TaskBundleTests(unittest.TestCase):
    def _write_task(self, root: Path, payload: dict) -> Path:
        path = root / "tasks.json"
        path.write_text(json.dumps([payload]), encoding="utf-8")
        return path

    def test_materializes_inputs_context_and_privacy_policies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("immutable input", encoding="utf-8")
            (root / "USER.md").write_text("private user", encoding="utf-8")
            (root / "SYSTEM.md").write_text("system rules", encoding="utf-8")
            (root / "SECRET.md").write_text("ephemeral context", encoding="utf-8")
            task_path = self._write_task(
                root,
                {
                    "schema_version": "roller.task/v1",
                    "id": "bundle-task",
                    "prompt": "Use inputs/input.txt and write outputs/report.md",
                    "files": {
                        "inputs": [{"source": "input.txt", "target": "input.txt"}],
                        "context": [
                            {"role": "user", "source": "USER.md"},
                            {"role": "system", "source": "SYSTEM.md"},
                            {
                                "role": "custom",
                                "source": "SECRET.md",
                                "target": "private/SECRET.md",
                                "capture": "none",
                            },
                        ],
                        "outputs": [{"path": "report.md", "required": True}],
                    },
                },
            )
            task = load_tasks(task_path)[0]
            store = TaskBundleStore(root / "run")
            bundle = store.prepare(task)

            self.assertEqual((bundle.inputs_root / "input.txt").read_text(), "immutable input")
            self.assertEqual((bundle.context_root / "USER.md").read_text(), "private user")
            input_manifest = json.loads(bundle.input_manifest_path.read_text())
            context_manifest = json.loads(bundle.context_manifest_path.read_text())
            task_manifest = json.loads(bundle.manifest_path.read_text())

            self.assertEqual(input_manifest["files"][0]["sha256"], hashlib.sha256(b"immutable input").hexdigest())
            user_file = context_manifest["declarations"][0]["files"][0]
            system_file = context_manifest["declarations"][1]["files"][0]
            private_file = context_manifest["declarations"][2]["files"][0]
            self.assertIn("sha256", user_file)
            self.assertNotIn("blob", user_file)
            self.assertIn("blob", system_file)
            self.assertNotIn("sha256", private_file)
            self.assertNotIn("blob", private_file)
            self.assertNotIn(str(root), json.dumps(task_manifest))
            self.assertFalse(bundle.resume_safe)

            store.cleanup_runtime()
            self.assertFalse((root / "run" / ".runtime").exists())

    def test_reuses_same_input_tree_between_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "shared.txt").write_text("same", encoding="utf-8")
            task_file = root / "tasks.json"
            task_file.write_text(
                json.dumps(
                    [
                        {"id": "a", "prompt": "a", "files": {"inputs": ["shared.txt"]}},
                        {"id": "b", "prompt": "b", "files": {"inputs": ["shared.txt"]}},
                    ]
                ),
                encoding="utf-8",
            )
            tasks = load_tasks(task_file)
            bundles = TaskBundleStore(root / "run").prepare_all(tasks)
            self.assertEqual(bundles["a"].inputs_root, bundles["b"].inputs_root)
            self.assertEqual(len(list((root / "run" / "blobs" / "sha256").iterdir())), 1)

    def test_rejects_path_traversal_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "file.txt").write_text("x", encoding="utf-8")
            (source / "link.txt").symlink_to(source / "file.txt")
            with self.assertRaisesRegex(TaskFormatError, "parent traversal"):
                load_tasks(
                    self._write_task(
                        root,
                        {
                            "id": "bad",
                            "prompt": "bad",
                            "files": {
                                "inputs": [{"source": "source/file.txt", "target": "../escape"}]
                            },
                        },
                    )
                )

            task = load_tasks(
                self._write_task(
                    root,
                    {"id": "link", "prompt": "x", "files": {"inputs": ["source"]}},
                )
            )[0]
            with self.assertRaisesRegex(BundleError, "symbolic link"):
                TaskBundleStore(root / "run").prepare(task)


if __name__ == "__main__":
    unittest.main()
