"""Opt-in HTTPS apt regression using the Dockerfiles' default Node slim images.

Pull node:22.22.0-bookworm-slim and node:22-bookworm-slim first, then run:
ROLLER_DOCKER_BUILD_NETWORK_TEST=1 python -m unittest discover \
    -s tests -p test_docker_build_https.py -v

Requires Docker and HTTPS access to deb.debian.org. Proxy environment variables
are forwarded. Set ROLLER_DOCKER_TEST_NETWORK=host for a localhost proxy on Linux.
No model API is called.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import unittest
import uuid

from test_docker_build_config import NODE_HARNESSES, instructions


@unittest.skipUnless(
    os.environ.get("ROLLER_DOCKER_BUILD_NETWORK_TEST") == "1",
    "set ROLLER_DOCKER_BUILD_NETWORK_TEST=1 to test HTTPS apt in Docker",
)
class NodeSlimHttpsTests(unittest.TestCase):
    def test_default_images_can_install_ca_certificates_from_https(self):
        for harness in NODE_HARNESSES:
            with self.subTest(harness=harness):
                commands = instructions(harness)
                defaults = dict(
                    body.split("=", 1) for kind, body in commands
                    if kind == "ARG" and "=" in body
                )
                image = next(body for kind, body in commands if kind == "FROM")
                image = image.replace("${BASE_IMAGE}", defaults.get("BASE_IMAGE", ""))
                prefix = []
                for kind, body in commands:
                    if kind != "RUN":
                        continue
                    if "apt-get" in body:
                        # Check the actual package set without downloading all
                        # Harness dependencies or installing npm packages.
                        packages = body.split("apt-get install -y --no-install-recommends", 1)[1]
                        packages = packages.split("&&", 1)[0].strip()
                        break
                    # Separate shells match Docker RUN boundaries.
                    prefix.append("/bin/sh -c " + shlex.quote(body))
                else:
                    self.fail(f"No apt install found for {harness}")
                script = "set -eu\n" + "\n".join(prefix) + "\n" + (
                    "test -s /etc/ssl/certs/ca-certificates.crt\n"
                    "apt-get -o APT::Update::Error-Mode=any -o Acquire::Retries=0 "
                    "-o Acquire::https::Timeout=30 update\n"
                    "apt-get -o Acquire::Retries=0 -o Acquire::https::Timeout=30 "
                    "install -y --no-install-recommends ca-certificates\n"
                    "test -s /etc/ssl/certs/ca-certificates.crt\n"
                    "dpkg-query -W -f='${Status}' ca-certificates\n"
                    "apt-get --simulate install -y --no-install-recommends " + packages + "\n"
                )
                name = "roller-https-" + uuid.uuid4().hex[:12]
                command = [
                    "docker", "run", "--rm", "--pull=never", "--name", name,
                    "--entrypoint", "/bin/sh",
                    "-e", "DEBIAN_MIRROR=https://deb.debian.org/debian",
                    "-e", "DEBIAN_SECURITY_MIRROR=https://deb.debian.org/debian-security",
                    "-e", "DEBIAN_FRONTEND=noninteractive",
                ]
                for variable in ("http_proxy", "https_proxy", "no_proxy",
                                 "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
                    if variable in os.environ:
                        command.extend(["-e", variable])
                if network := os.environ.get("ROLLER_DOCKER_TEST_NETWORK"):
                    command.extend(["--network", network])
                command.extend([image, "-c", script])
                try:
                    result = subprocess.run(command, capture_output=True, text=True, timeout=240)
                    output = result.stdout + result.stderr
                    self.assertEqual(result.returncode, 0, output[-12000:])
                    self.assertIn("install ok installed", output)
                    self.assertIn("https://deb.debian.org/debian", output)
                    self.assertNotIn("Certificate verification failed", output)
                    self.assertNotIn("No system certificates available", output)
                finally:
                    subprocess.run(
                        ["docker", "rm", "-f", name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                    )


if __name__ == "__main__":
    unittest.main()
