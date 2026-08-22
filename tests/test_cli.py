from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from roller.cli import _docker_preflight


class CliTests(unittest.TestCase):
    @patch("roller.cli.subprocess.run")
    @patch("roller.cli.shutil.which", return_value="/usr/bin/docker")
    def test_docker_preflight_returns_resolved_image_id(self, _which, run) -> None:
        run.side_effect = [
            subprocess.CompletedProcess([], 0, "27.5.1\n", ""),
            subprocess.CompletedProcess([], 0, "sha256:image-id\n", ""),
        ]

        digest = _docker_preflight("docker", "openclaw:test")

        self.assertEqual(digest, "sha256:image-id")

    @patch("roller.cli.subprocess.run")
    @patch("roller.cli.shutil.which", return_value="/usr/bin/docker")
    def test_docker_preflight_requires_a_local_image(self, _which, run) -> None:
        run.side_effect = [
            subprocess.CompletedProcess([], 0, "27.5.1\n", ""),
            subprocess.CompletedProcess([], 1, "", "No such image"),
        ]

        with self.assertRaisesRegex(RuntimeError, "pull or load it first"):
            _docker_preflight("docker", "missing:test")


if __name__ == "__main__":
    unittest.main()
