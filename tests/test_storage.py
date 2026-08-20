from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from roller.storage import write_jsonl


class JsonlStorageTests(unittest.TestCase):
    def test_writes_jsonl_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dataset.jsonl"
            write_jsonl(path, ({"index": index} for index in range(2)))

            self.assertEqual(
                [json.loads(line) for line in path.read_text().splitlines()],
                [{"index": 0}, {"index": 1}],
            )

    def test_generator_failure_preserves_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "dataset.jsonl"
            path.write_text("original\n", encoding="utf-8")

            def broken_rows():
                yield {"index": 0}
                raise RuntimeError("broken")

            with self.assertRaisesRegex(RuntimeError, "broken"):
                write_jsonl(path, broken_rows())
            self.assertEqual(path.read_text(), "original\n")
            self.assertEqual({item.name for item in root.iterdir()}, {"dataset.jsonl"})


if __name__ == "__main__":
    unittest.main()
