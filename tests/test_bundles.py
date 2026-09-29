from __future__ import annotations

import hashlib
from collections import Counter
import json
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

from roller.bundles import BundleError, TaskBundleStore, _canonical_digest
from roller.models import ContextFile, InputFile, Task
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
            with patch("roller.bundles._canonical_digest", wraps=_canonical_digest) as digest:
                bundle = store.prepare(task)

            self.assertEqual(
                (bundle.inputs_root / "inputs" / "input.txt").read_text(),
                "immutable input",
            )
            self.assertEqual((bundle.context_root / "USER.md").read_text(), "private user")
            manifests = {
                call.args[0]["schema_version"]: call.args[0]
                for call in digest.call_args_list
                if "schema_version" in call.args[0]
            }
            input_manifest = manifests["roller.inputs/v1"]
            context_manifest = manifests["roller.context/v1"]
            task_manifest = manifests["roller.task/v1"]

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
            self.assertFalse((root / "run" / "blobs").exists())
            self.assertEqual(len(list((root / "run" / "trees" / "sha256").iterdir())), 1)

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
            with patch("roller.bundles._canonical_digest", wraps=_canonical_digest) as digest:
                bundle = TaskBundleStore(root / "run").prepare(task)

            self.assertEqual((bundle.inputs_root / "renamed.txt").read_text(), "source")
            self.assertEqual(
                (bundle.inputs_root / "nested" / "config.yaml").read_text(),
                "a: 1\n",
            )
            manifest = digest.call_args.args[0]
            self.assertEqual(manifest["files"]["input_mode"], "copy")

    @staticmethod
    def _compatibility_task(root: Path) -> Task:
        (root / "inputs" / "empty").mkdir(parents=True)
        (root / "inputs" / "run.sh").write_bytes(b"#!/bin/sh\necho hello\n")
        (root / "inputs" / "run.sh").chmod(0o755)
        for name in ("full", "hash", "none"):
            (root / name).write_bytes(name.encode())
            (root / name).chmod(0o640)
        return Task(
            id="compatibility",
            prompts=("read inputs",),
            source_index=0,
            inputs=(
                InputFile(root / "inputs", "data"),
                InputFile(None, "inline.txt", content="你好\n"),
                InputFile(root / "missing", "optional", required=False),
            ),
            context_files=tuple(
                ContextFile("custom", root / name, name + ".md", capture=name)
                for name in ("full", "hash", "none")
            ),
            input_mode="copy",
        )

    def test_preserves_legacy_bundle_digests(self) -> None:
        # Recorded from main at 0a9e0df, with explicit modes for portability.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = self._compatibility_task(root)
            bundle = TaskBundleStore(root / "bundle").prepare(task)
            self.assertEqual(
                (bundle.input_tree_digest, bundle.input_manifest_digest,
                 bundle.context_manifest_digest, bundle.manifest_digest),
                ("17519be61a1bf07237724653f7ed362ba3b3b0850d6146718a9c72fa7e7eefbf",
                 "12c075febcb964b535988ad3e9507978d48e27d414ff180446d774c409e9f32d",
                 "880add335e25c1016321eeecbd1e02dff525b3ccb60d2ecb88f915bd99e4084b",
                 "36a1cf8a539a95f01e05ca61673fa6179605bc799d8c2c325324a53fc2581e84"),
            )
            self.assertTrue((bundle.inputs_root / "data" / "empty").is_dir())
            self.assertEqual((bundle.inputs_root / "data" / "run.sh").stat().st_mode & 0o777, 0o755)
            self.assertEqual((bundle.context_root / "full.md").stat().st_mode & 0o777, 0o640)

    def test_snapshots_read_sources_twice_and_write_only_final_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = self._compatibility_task(root)
            # Cross the stream chunk boundary and include arbitrary binary data.
            (root / "inputs" / "run.sh").write_bytes(bytes(range(256)) * 8193)
            store = TaskBundleStore(root / "bundle")
            opened = Counter()
            original_open = Path.open

            def counted_open(path, mode="r", *args, **kwargs):
                opened[path, mode] += 1
                return original_open(path, mode, *args, **kwargs)

            with patch.object(Path, "open", counted_open):
                bundle = store.prepare(task)

            sources = [root / "inputs" / "run.sh", *(root / name for name in ("full", "hash", "none"))]
            self.assertEqual(
                Counter({path: count for (path, mode), count in opened.items() if mode == "rb"}),
                Counter({path: 2 for path in sources}),
            )
            writes = {path: count for (path, mode), count in opened.items() if mode == "wb"}
            self.assertEqual(len(writes), 5)  # Four source files plus one inline input.
            self.assertTrue(all(count == 1 for count in writes.values()))
            self.assertEqual(len(opened), len(sources) + len(writes))
            stored_files = [path for path in store.run_dir.rglob("*") if path.is_file()]
            self.assertEqual(len(stored_files), 5)
            self.assertEqual(
                sum(path.stat().st_size for path in stored_files),
                sum(path.stat().st_size for path in sources) + len("你好\n".encode()),
            )
            self.assertFalse((store.run_dir / "blobs").exists())
            self.assertFalse((store.run_dir / "tasks").exists())
            self.assertEqual((bundle.inputs_root / "data" / "run.sh").read_bytes(), sources[0].read_bytes())
            for name in ("full", "hash", "none"):
                self.assertEqual((bundle.context_root / (name + ".md")).read_bytes(), name.encode())
            # A reused input tree performs no additional input I/O.
            opened.clear()
            with patch.object(Path, "open", counted_open):
                store.prepare(task)
            self.assertNotIn((sources[0], "rb"), opened)
            self.assertEqual(sum(count for (_, mode), count in opened.items() if mode == "wb"), 3)

    def test_inline_snapshot_memory_is_bounded_for_new_and_reused_trees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Create strings before tracing, as Task already owns them at prepare time.
            task = Task(
                id="inline-memory",
                prompts=("read",),
                source_index=0,
                inputs=tuple(
                    InputFile(None, f"{index}.txt", content=f"{index}:" + "你好" * 32768)
                    for index in range(32)
                ),
            )
            largest_file = max(len(spec.content.encode("utf-8")) for spec in task.inputs)
            store = TaskBundleStore(root / "bundle")
            first_bundle = None
            for reused in (False, True):
                with self.subTest(reused=reused):
                    tracemalloc.start()
                    try:
                        bundle = store.prepare(task)
                        _, peak = tracemalloc.get_traced_memory()
                    finally:
                        tracemalloc.stop()
                    # Allow metadata and transient allocations, but not all 32 encodings.
                    self.assertLess(peak, largest_file * 4)
                    if first_bundle is not None:
                        self.assertEqual(bundle.inputs_root, first_bundle.inputs_root)
                        self.assertEqual(bundle.manifest_digest, first_bundle.manifest_digest)
                    first_bundle = bundle
            for spec in task.inputs:
                self.assertEqual(
                    (bundle.inputs_root / spec.path).read_bytes(), spec.content.encode("utf-8")
                )

    def test_rejects_source_changes_and_cleans_partial_snapshots(self) -> None:
        for kind in ("input", "full", "hash", "none"):
            for changed in (b"other", b"", b"longer content"):
                with self.subTest(kind=kind, changed=changed), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    source = root / "source.txt"
                    source.write_bytes(b"first")
                    task = Task(
                        id="changed", prompts=("read",), source_index=0,
                        inputs=(InputFile(source, "input.txt"),) if kind == "input" else (),
                        context_files=() if kind == "input" else (
                            ContextFile("custom", source, "context.txt", capture=kind),
                        ),
                    )
                    store = TaskBundleStore(root / "bundle")
                    scan_source = store._scan_source

                    def scan_then_change(*args, **kwargs):
                        scanned = scan_source(*args, **kwargs)
                        source.write_bytes(changed)
                        return scanned

                    with patch.object(store, "_scan_source", side_effect=scan_then_change):
                        with self.assertRaisesRegex(BundleError, "changed while snapshotting"):
                            store.prepare(task)
                    self.assertFalse(any(path.is_file() for path in store.run_dir.rglob("*")))
                    self.assertFalse(any(path.name.startswith(".") for path in store.trees_root.iterdir()))
                    # A fresh snapshot can be built after the failed attempt.
                    bundle = TaskBundleStore(store.run_dir).prepare(task)
                    destination = (bundle.inputs_root / "input.txt" if kind == "input"
                                   else bundle.context_root / "context.txt")
                    self.assertEqual(destination.read_bytes(), changed)

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
