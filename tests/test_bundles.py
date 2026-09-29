from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            (root / "inputs").mkdir()
            (root / "inputs" / "input.txt").write_text(
                "immutable input", encoding="utf-8"
            )
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
                        "inputs": [{"source": "inputs/input.txt"}],
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
                        "output_dir": "outputs",
                    },
                },
            )
            task = load_tasks(task_path)[0]
            store = TaskBundleStore(root / "run")
            bundle = store.prepare(task)

            self.assertEqual(
                (bundle.inputs_root / "inputs" / "input.txt").read_text(),
                "immutable input",
            )
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
            self.assertEqual(task_manifest["files"]["output_collection"], "all")
            self.assertFalse(bundle.resume_safe)

            store.cleanup_runtime()
            self.assertFalse((root / "run" / ".runtime").exists())

    def test_persists_only_full_context_with_an_audit_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "skills" / "nested").mkdir(parents=True)
            (root / "skills" / "a.md").write_text("shared full", encoding="utf-8")
            (root / "skills" / "nested" / "b.md").write_text(
                "shared full", encoding="utf-8"
            )
            (root / "USER.md").write_text("hash only", encoding="utf-8")
            (root / "SECRET.md").write_text("not captured", encoding="utf-8")
            for path in (
                root / "skills" / "a.md",
                root / "skills" / "nested" / "b.md",
                root / "USER.md",
                root / "SECRET.md",
            ):
                path.chmod(0o640)
            task = load_tasks(
                self._write_task(
                    root,
                    {
                        "id": "context-archive",
                        "prompt": "read context",
                        "files": {
                            "context": [
                                {
                                    "role": "skill",
                                    "source": "skills",
                                    "target": "skills/demo",
                                    "capture": "full",
                                },
                                {
                                    "role": "user",
                                    "source": "USER.md",
                                    "capture": "hash",
                                },
                                {
                                    "role": "custom",
                                    "source": "SECRET.md",
                                    "target": "SECRET.md",
                                    "capture": "none",
                                },
                            ]
                        },
                    },
                )
            )[0]
            store = TaskBundleStore(root / "bundle")
            bundle = store.prepare(task)
            runtime_manifest = bundle.context_manifest_path.read_bytes()
            self.assertEqual(
                bundle.context_manifest_digest,
                "e862fef694a6c064af66fdab13d9da0435d1f33d25999278787fe28a4ca721bb",
            )
            destination = root / "saved-run"

            with patch.object(
                store, "_persist_blob", wraps=store._persist_blob
            ) as persist_blob:
                store.persist_full_context([bundle], destination)
            self.assertEqual(persist_blob.call_count, 1)

            full_digest = hashlib.sha256(b"shared full").hexdigest()
            hash_digest = hashlib.sha256(b"hash only").hexdigest()
            none_digest = hashlib.sha256(b"not captured").hexdigest()
            blob = destination / "context" / "sha256" / full_digest
            self.assertEqual(blob.read_text(encoding="utf-8"), "shared full")
            self.assertEqual(
                sorted(path.name for path in blob.parent.iterdir()), [full_digest]
            )
            self.assertFalse((blob.parent / hash_digest).exists())
            self.assertFalse((blob.parent / none_digest).exists())

            archive_path = (
                destination
                / "context"
                / "manifests"
                / f"{bundle.context_manifest_digest}.json"
            )
            archive = json.loads(archive_path.read_text(encoding="utf-8"))
            self.assertEqual(archive["schema_version"], "roller.context-archive/v1")
            self.assertEqual(
                archive["context_manifest_digest"], bundle.context_manifest_digest
            )
            self.assertEqual(
                [declaration["role"] for declaration in archive["declarations"]],
                ["skill"],
            )
            self.assertEqual(
                {record["blob"] for record in archive["declarations"][0]["files"]},
                {f"context/sha256/{full_digest}"},
            )
            self.assertEqual(bundle.context_manifest_path.read_bytes(), runtime_manifest)

            mtimes = (blob.stat().st_mtime_ns, archive_path.stat().st_mtime_ns)
            store.persist_full_context([bundle], destination)
            self.assertEqual(
                (blob.stat().st_mtime_ns, archive_path.stat().st_mtime_ns), mtimes
            )

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

    def test_materializes_renamed_and_inline_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.txt").write_text("source", encoding="utf-8")
            task = load_tasks(
                self._write_task(
                    root,
                    {
                        "id": "inline",
                        "prompt": "read",
                        "files": {
                            "inputs": [
                                {"source": "source.txt", "target": "renamed.txt"},
                                {"path": "nested/config.yaml", "content": "a: 1\n"},
                            ],
                            "input_mode": "copy",
                        },
                    },
                )
            )[0]
            bundle = TaskBundleStore(root / "run").prepare(task)

            self.assertEqual((bundle.inputs_root / "renamed.txt").read_text(), "source")
            self.assertEqual(
                (bundle.inputs_root / "nested" / "config.yaml").read_text(),
                "a: 1\n",
            )
            manifest = json.loads(bundle.manifest_path.read_text())
            self.assertEqual(manifest["files"]["input_mode"], "copy")

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
                                "inputs": [{"source": "../escape"}]
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
