from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from roller.artifacts import collect_output_artifacts
from roller.models import OutputFile


class ArtifactCollectionTests(unittest.TestCase):
    def test_saves_only_declared_outputs_in_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging"
            sample = root / "sample"
            (staging / "assets").mkdir(parents=True)
            (staging / "empty").mkdir()
            (staging / "report.md").write_text("report", encoding="utf-8")
            (staging / "assets" / "chart.json").write_text("{}", encoding="utf-8")
            (staging / "debug.tmp").write_text("debug", encoding="utf-8")

            result = collect_output_artifacts(
                staging,
                sample,
                (
                    OutputFile("report.md", "file", True),
                    OutputFile("assets", "directory", True),
                    OutputFile("empty", "directory", True),
                ),
            )

            self.assertTrue(result.passed)
            self.assertTrue((sample / "results" / "report.md").is_file())
            self.assertTrue(
                (sample / "results" / "assets" / "chart.json").is_file()
            )
            self.assertFalse((sample / "results" / "debug.tmp").exists())
            self.assertTrue((sample / "results" / "empty").is_dir())
            self.assertEqual(result.declared_files, 2)
            self.assertEqual(result.ignored_files, 1)
            self.assertEqual(len(result.files), 2)

    def test_without_declarations_does_not_create_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging"
            staging.mkdir()
            (staging / "debug.txt").write_text("ignored", encoding="utf-8")

            result = collect_output_artifacts(staging, root / "sample", ())

            self.assertTrue(result.passed)
            self.assertIsNone(result.results_path)
            self.assertFalse((root / "sample" / "results").exists())

    def test_reports_missing_oversized_and_unsafe_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging"
            staging.mkdir()
            (staging / "large.bin").write_bytes(b"12345")
            (staging / "unsafe").symlink_to(root / "outside")
            result = collect_output_artifacts(
                staging,
                root / "sample",
                (
                    OutputFile("missing.md", "file", True),
                    OutputFile("large.bin", "file", True, max_bytes=2),
                ),
            )
            error_types = {error["type"] for error in result.errors}
            self.assertFalse(result.passed)
            self.assertEqual(
                error_types,
                {"required_output_missing", "output_too_large", "unsafe_symlink"},
            )


if __name__ == "__main__":
    unittest.main()
