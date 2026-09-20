from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from roller.artifacts import collect_output_directory


class ArtifactCollectionTests(unittest.TestCase):
    def test_saves_every_output_in_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging"
            sample = root / "sample"
            (staging / "assets").mkdir(parents=True)
            (staging / "empty").mkdir()
            (staging / "report.md").write_text("report", encoding="utf-8")
            (staging / "assets" / "chart.json").write_text("{}", encoding="utf-8")
            (staging / "debug.tmp").write_text("debug", encoding="utf-8")

            result = collect_output_directory(staging, sample)

            self.assertTrue(result.passed)
            self.assertTrue((sample / "results" / "report.md").is_file())
            self.assertTrue(
                (sample / "results" / "assets" / "chart.json").is_file()
            )
            self.assertTrue((sample / "results" / "debug.tmp").is_file())
            self.assertTrue((sample / "results" / "empty").is_dir())
            self.assertEqual(result.file_count, 3)
            self.assertEqual(result.directory_count, 2)
            self.assertEqual(len(result.files), 3)

    def test_empty_output_directory_does_not_create_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging"
            staging.mkdir()
            result = collect_output_directory(staging, root / "sample")

            self.assertTrue(result.passed)
            self.assertIsNone(result.results_path)
            self.assertFalse((root / "sample" / "results").exists())

    def test_rejects_unsafe_outputs_while_preserving_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging"
            staging.mkdir()
            (staging / "large.bin").write_bytes(b"12345")
            (staging / "unsafe").symlink_to(root / "outside")
            result = collect_output_directory(staging, root / "sample")
            error_types = {error["type"] for error in result.errors}
            self.assertFalse(result.passed)
            self.assertEqual(error_types, {"unsafe_symlink"})
            self.assertTrue((root / "sample" / "results" / "large.bin").is_file())

    def test_excludes_generated_node_modules_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging"
            sample = root / "sample"
            (staging / "app" / "node_modules" / ".bin").mkdir(parents=True)
            (staging / "app" / "node_modules" / "mime").mkdir()
            (staging / "app" / "node_modules" / "mime" / "cli.js").write_text(
                "module", encoding="utf-8"
            )
            (staging / "app" / "node_modules" / ".bin" / "mime").symlink_to(
                "../mime/cli.js"
            )
            (staging / "app" / "index.js").write_text("source", encoding="utf-8")

            result = collect_output_directory(staging, sample)

            self.assertTrue(result.passed)
            self.assertEqual([row["path"] for row in result.files], ["app/index.js"])
            self.assertFalse((sample / "results" / "app" / "node_modules").exists())


if __name__ == "__main__":
    unittest.main()
