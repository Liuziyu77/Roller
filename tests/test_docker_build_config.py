from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import ssl
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HARNESSES = ("codex", "claude-code", "opencode", "pi", "deepseek", "hermes")
MAIN = "http://deb.debian.org/debian"
SECURITY = "http://deb.debian.org/debian-security"
NODE_HARNESSES = ("codex", "claude-code", "pi")


def instructions(harness: str) -> list[tuple[str, str]]:
    """Read shell-form instructions, including continued RUN commands."""
    result = []
    pending = ""
    for line in (ROOT / "docker" / harness / "Dockerfile").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        pending += line.rstrip().removesuffix("\\") + " "
        if line.rstrip().endswith("\\"):
            continue
        kind, body = pending.strip().split(None, 1)
        result.append((kind, body))
        pending = ""
    return result


@unittest.skipUnless(shutil.which("node"), "Node is needed to check CA bootstrapping")
class NodeCertificateTests(unittest.TestCase):
    def bootstrap(self, harness, certificate_directory, *, path=None):
        commands = instructions(harness)
        index, command = next(
            (index, body) for index, (kind, body) in enumerate(commands)
            if kind == "RUN" and "rootCertificates" in body
        )
        for later, (kind, body) in enumerate(commands):
            if kind == "RUN" and ("DEBIAN_MIRROR" in body or "apt-get" in body):
                self.assertLess(index, later, "CA bootstrap must precede mirror setup and apt")
        return subprocess.run(
            ["/bin/sh", "-c", command.replace("/etc/ssl/certs", str(certificate_directory))],
            env={"PATH": path or os.environ["PATH"]},
            capture_output=True, text=True, timeout=10,
        )

    def test_missing_or_empty_bundle_contains_usable_certificates(self):
        for harness in NODE_HARNESSES:
            for empty in (False, True):
                with self.subTest(harness=harness, empty=empty), tempfile.TemporaryDirectory() as directory:
                    certs = Path(directory) / "certs"
                    bundle = certs / "ca-certificates.crt"
                    if empty:
                        certs.mkdir()
                        bundle.touch()
                    result = self.bootstrap(harness, certs)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    context.load_verify_locations(cafile=str(bundle))
                    self.assertGreater(context.cert_store_stats()["x509_ca"], 0)

    def test_custom_bundle_is_preserved_without_running_node(self):
        for harness in NODE_HARNESSES:
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as directory:
                certs = Path(directory)
                bundle = certs / "ca-certificates.crt"
                original = b"existing custom trust bundle\n"
                bundle.write_bytes(original)
                # An existing bundle requires neither Node nor other programs.
                result = self.bootstrap(harness, certs, path=str(certs / "no-programs"))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(bundle.read_bytes(), original)

    def test_node_failure_stops_bootstrap(self):
        for harness in NODE_HARNESSES:
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                binary = root / "node"
                binary.write_text("#!/bin/sh\nexit 42\n")
                binary.chmod(0o755)
                result = self.bootstrap(harness, root / "certs", path=str(root) + os.pathsep + os.defpath)
                self.assertEqual(result.returncode, 42, result.stderr)


class DebianMirrorTests(unittest.TestCase):
    def rewrite(self, harness, text, settings, filename="sources.list.d/debian.sources"):
        commands = instructions(harness)
        args = {body.split("=", 1)[0] for kind, body in commands if kind == "ARG"}
        self.assertTrue({"DEBIAN_MIRROR", "DEBIAN_SECURITY_MIRROR"} <= args)
        command = next(body for kind, body in commands if kind == "RUN" and "DEBIAN_MIRROR" in body)
        # Execute the Dockerfile's own shell code against temporary apt files.
        # Package installation must remain in a later instruction.
        self.assertNotIn("apt-get", command)
        with tempfile.TemporaryDirectory() as directory:
            apt = Path(directory) / "apt"
            path = apt / filename
            path.parent.mkdir(parents=True)
            path.write_text(text)
            ignored = path.parent / "local.list.save"
            ignored.write_text(text)
            environment = {"PATH": os.defpath, **settings}
            result = subprocess.run(
                ["sh", "-c", command.replace("/etc/apt", str(apt))],
                env=environment, text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(ignored.read_text(), text)
            return path.read_text()

    def test_independent_mirrors(self):
        cases = (
            ({}, MAIN, SECURITY),
            ({"DEBIAN_MIRROR": "", "DEBIAN_SECURITY_MIRROR": ""}, MAIN, SECURITY),
            ({"DEBIAN_MIRROR": "http://host/repository/debian-proxy"}, "http://host/repository/debian-proxy", SECURITY),
            ({"DEBIAN_SECURITY_MIRROR": "https://security.example/archive"}, MAIN, "https://security.example/archive"),
            ({"DEBIAN_MIRROR": "http://host/repository/debian-proxy", "DEBIAN_SECURITY_MIRROR": "http://host/repository/debian-security-proxy"}, "http://host/repository/debian-proxy", "http://host/repository/debian-security-proxy"),
            ({"DEBIAN_MIRROR": "https://main.example/packages", "DEBIAN_SECURITY_MIRROR": "https://security.example/archive"}, "https://main.example/packages", "https://security.example/archive"),
            ({"DEBIAN_MIRROR": "https://main.example/packages", "DEBIAN_SECURITY_MIRROR": SECURITY}, "https://main.example/packages", SECURITY),
            ({"DEBIAN_MIRROR": MAIN, "DEBIAN_SECURITY_MIRROR": "https://security.example/archive"}, MAIN, "https://security.example/archive"),
            ({"DEBIAN_MIRROR": "https://mirror.example/debian", "DEBIAN_SECURITY_MIRROR": "https://mirror.example/debian-security"}, "https://mirror.example/debian", "https://mirror.example/debian-security"),
            ({"DEBIAN_MIRROR": "https://main.example/packages/", "DEBIAN_SECURITY_MIRROR": "https://security.example/archive/"}, "https://main.example/packages", "https://security.example/archive"),
        )
        original = f"Types: deb\nURIs: {MAIN}\nSuites: bookworm bookworm-updates\n\nTypes: deb\nURIs: {SECURITY}\nSuites: bookworm-security\n"
        for harness in HARNESSES:
            for settings, main, security in cases:
                with self.subTest(harness=harness, settings=settings):
                    expected = f"Types: deb\nURIs: {main}\nSuites: bookworm bookworm-updates\n\nTypes: deb\nURIs: {security}\nSuites: bookworm-security\n"
                    self.assertEqual(self.rewrite(harness, original, settings), expected)

    def test_legacy_lists_https_trailing_slashes_and_url_boundaries(self):
        original = (
            "deb https://deb.debian.org/debian/ bookworm main\n"
            "deb-src [signed-by=/keys/debian.gpg] https://security.debian.org/debian-security/ bookworm-security main\n"
            "deb http://deb.debian.org/debian-extra bookworm main\n"
            "deb http://deb.debian.org/debian-security-archive bookworm main\n"
            "deb http://archive.ubuntu.com/ubuntu noble main\n"
            "URIs: https://deb.debian.org/debian https://deb.debian.org/debian-security\n"
            "URIs: https://deb.debian.org/debian"
        )
        expected = (
            "deb https://main.example/repo bookworm main\n"
            "deb-src [signed-by=/keys/debian.gpg] https://security.example/repo bookworm-security main\n"
            "deb http://deb.debian.org/debian-extra bookworm main\n"
            "deb http://deb.debian.org/debian-security-archive bookworm main\n"
            "deb http://archive.ubuntu.com/ubuntu noble main\n"
            "URIs: https://main.example/repo https://security.example/repo\n"
            "URIs: https://main.example/repo"
        )
        for harness in HARNESSES:
            for filename in ("sources.list", "sources.list.d/custom.list", "sources.list.d/custom.sources"):
                with self.subTest(harness=harness, filename=filename):
                    actual = self.rewrite(harness, original, {"DEBIAN_MIRROR": "https://main.example/repo", "DEBIAN_SECURITY_MIRROR": "https://security.example/repo"}, filename)
                    self.assertEqual(actual, expected)

    def test_replacement_metacharacters_are_literal(self):
        # sed treats &, backslashes and the delimiter specially in replacements.
        mirror = r"https://mirror.example/repo?a=1&b=2|literal@host\suffix"
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                self.assertEqual(
                    self.rewrite(harness, f"URIs: {MAIN}\nURIs: {SECURITY}\n", {"DEBIAN_MIRROR": mirror, "DEBIAN_SECURITY_MIRROR": mirror}),
                    f"URIs: {mirror}\nURIs: {mirror}\n",
                )

    def test_custom_base_without_debian_sources(self):
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                self.assertEqual(self.rewrite(harness, "unchanged", {"DEBIAN_MIRROR": "https://mirror.example/repo"}, "unrelated.conf"), "unchanged")


@unittest.skipUnless(shutil.which("npm"), "npm is needed to check registry resolution")
class NpmRegistryTests(unittest.TestCase):
    def test_install_uses_override_or_existing_npm_configuration(self):
        npm = shutil.which("npm")
        for harness in ("codex", "claude-code", "pi"):
            commands = instructions(harness)
            self.assertIn(("ARG", "NPM_REGISTRY"), commands)
            command = next(body for kind, body in commands if kind == "RUN" and "npm install" in body)
            command = command[command.index('if [ -n "${NPM_REGISTRY:-}"'):]
            for override in (None, "", "https://registry.example/repository/npm-proxy/"):
                with self.subTest(harness=harness, override=override), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    userconfig = root / "npmrc"
                    userconfig.write_text("registry=https://base.example/npm/\n")
                    globalconfig = root / "global-npmrc"
                    globalconfig.write_text("")
                    bin_dir = root / "bin"
                    bin_dir.mkdir()
                    # Run npm's real config resolution at the install call site,
                    # without installing packages or reaching a registry.
                    wrapper = bin_dir / "npm"
                    wrapper.write_text("#!/bin/sh\nif [ \"$1\" = install ]; then\n  exec " + shlex.quote(npm) + " config get registry\nfi\n")
                    wrapper.chmod(0o755)
                    for tool in ("codex", "claude"):
                        stub = bin_dir / tool
                        stub.write_text("#!/bin/sh\nexit 0\n")
                        stub.chmod(0o755)
                    environment = {
                        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                        "NPM_CONFIG_USERCONFIG": str(userconfig),
                        "NPM_CONFIG_GLOBALCONFIG": str(globalconfig),
                    }
                    if override is not None:
                        environment["NPM_REGISTRY"] = override
                    result = subprocess.run(["sh", "-c", command], cwd=root, env=environment, capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), override or "https://base.example/npm/")


class CodexApplyPatchTests(unittest.TestCase):
    def test_alias_supports_current_and_legacy_native_layouts(self):
        command = next(body for kind, body in instructions("codex")
                       if kind == "RUN" and "/usr/local/bin/apply_patch" in body)
        for layout in ("bin/codex", "codex/codex"):
            for executable in (True, False):
                with self.subTest(layout=layout, executable=executable), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    modules = root / "modules"
                    native = modules / "@openai/codex/node_modules/@openai/codex-linux-x64/vendor/target" / layout
                    native.parent.mkdir(parents=True)
                    native.write_text("#!/bin/sh\nprintf '%s\\n' alias-ok\n")
                    native.chmod(0o755 if executable else 0o644)
                    binary_dir = root / "bin"
                    binary_dir.mkdir()
                    npm = binary_dir / "npm"
                    npm.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(str(modules)) + "\n")
                    npm.chmod(0o755)
                    alias = binary_dir / "apply_patch"
                    result = subprocess.run(
                        ["sh", "-c", command.replace("/usr/local/bin/apply_patch", str(alias))],
                        env={"PATH": str(binary_dir) + os.pathsep + os.defpath},
                        capture_output=True, text=True, timeout=5,
                    )
                    if executable:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(alias.resolve(), native)
                        self.assertEqual(subprocess.check_output([str(alias)], text=True, timeout=5), "alias-ok\n")
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertFalse(alias.is_symlink())


if __name__ == "__main__":
    unittest.main()
