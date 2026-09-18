from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from roller.image_build import _build, _build_command_prefix, prepare_task_images
from roller.models import ContainerImageBuild, RunRequest, Task


class TaskImageBuildTests(unittest.TestCase):
    def tearDown(self) -> None:
        _build_command_prefix.cache_clear()

    def _request(self, root: Path, *, harness: str = "opencode") -> RunRequest:
        spec = ContainerImageBuild(
            image="roller-task:final",
            base_image="roller-task:base",
            context=root,
            dockerfile="FROM ubuntu:24.04\n",
            harness="opencode",
            build_args=(("EXAMPLE", "yes"),),
            timeout_seconds=42,
        )
        task = Task(
            id="task",
            prompts=("do it",),
            source_index=0,
            harness=harness,
            docker_image=spec.image,
            image_build=spec,
        )
        return RunRequest(0, task, "provider/model", 60)

    def test_builds_base_then_harness_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            request = self._request(Path(temporary))
            with (
                mock.patch(
                    "roller.image_build._inspect_image",
                    side_effect=[None, None, "sha256:final"],
                ),
                mock.patch("roller.image_build._build") as build,
            ):
                result = prepare_task_images(
                    "docker",
                    [request],
                    {"opencode": "roller-opencode:1.18.30"},
                )

            self.assertEqual(result, {"task": "sha256:final"})
            self.assertEqual(build.call_count, 2)
            self.assertEqual(build.call_args_list[0].kwargs["image"], "roller-task:base")
            self.assertEqual(build.call_args_list[1].kwargs["image"], "roller-task:final")
            overlay = build.call_args_list[1].kwargs["dockerfile_source"]
            self.assertIn("FROM ${HARNESS_IMAGE} AS roller_harness", overlay)
            self.assertIn("command -v python3", overlay)

    def test_reuses_final_image_without_rebuilding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            request = self._request(Path(temporary))
            with (
                mock.patch(
                    "roller.image_build._inspect_image",
                    side_effect=["sha256:cached", "sha256:cached"],
                ),
                mock.patch("roller.image_build._build") as build,
            ):
                result = prepare_task_images(
                    "docker",
                    [request],
                    {"opencode": "roller-opencode:1.18.30"},
                )

            build.assert_not_called()
            self.assertEqual(result, {"task": "sha256:cached"})

    def test_rejects_incompatible_harness_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            request = self._request(Path(temporary))
            with self.assertRaisesRegex(ValueError, "requires the opencode harness"):
                prepare_task_images(
                    "docker",
                    [request],
                    {"opencode": "image", "openclaw": "other"},
                    cli_harness="openclaw",
                )

    @mock.patch("roller.image_build.subprocess.run")
    def test_rootless_podman_builds_ignore_unmappable_layer_owners(self, run) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = "podman version 4.9.3\n"

        prefix = _build_command_prefix("docker")

        self.assertEqual(
            prefix,
            [
                "docker",
                "--storage-opt",
                "overlay.ignore_chown_errors=true",
                "build",
            ],
        )

    @mock.patch("roller.image_build.subprocess.run")
    def test_docker_build_prefix_is_unchanged(self, run) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = "Docker version 28.0.0\n"

        self.assertEqual(_build_command_prefix("docker"), ["docker", "build"])

    def test_build_passes_uppercase_and_lowercase_proxy_args(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch(
                    "roller.image_build._build_command_prefix",
                    return_value=["docker", "build"],
                ),
                mock.patch("roller.image_build.subprocess.run") as run,
                mock.patch.dict(
                    "roller.image_build.os.environ",
                    {"HTTPS_PROXY": "http://proxy.test:3128"},
                    clear=True,
                ),
            ):
                run.return_value.returncode = 0
                _build(
                    "docker",
                    image="example:test",
                    context=root,
                    dockerfile_source="FROM scratch\n",
                    build_args=(),
                    timeout_seconds=30,
                )

        command = run.call_args.args[0]
        self.assertIn("HTTPS_PROXY=http://proxy.test:3128", command)
        self.assertIn("https_proxy=http://proxy.test:3128", command)


if __name__ == "__main__":
    unittest.main()
