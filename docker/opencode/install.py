"""Install a versioned standalone OpenCode binary, checking npm's SHA-512."""
import base64
import hashlib
import io
import json
from pathlib import Path
import platform
import sys
import tarfile
from urllib.request import urlopen

version, registry = sys.argv[1:]
architecture = {"x86_64": "x64-baseline", "aarch64": "arm64"}.get(platform.machine())
if architecture is None:
    raise RuntimeError("OpenCode image supports Linux x86_64 and aarch64")
package = "opencode-linux-" + architecture
with urlopen(f"{registry.rstrip('/')}/{package}/{version}", timeout=60) as response:
    metadata = json.load(response)
if metadata.get("name") != package or metadata.get("version") != version:
    raise RuntimeError("unexpected OpenCode package metadata")
with urlopen(metadata["dist"]["tarball"], timeout=300) as response:
    archive = response.read()
actual = "sha512-" + base64.b64encode(hashlib.sha512(archive).digest()).decode()
if actual != metadata["dist"]["integrity"]:
    raise RuntimeError("OpenCode package SHA-512 mismatch")
with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package_archive:
    member = package_archive.getmember("package/bin/opencode")
    if not member.isfile():
        raise RuntimeError("OpenCode binary must be a regular file")
    destination = Path("/usr/local/bin/opencode")
    destination.write_bytes(package_archive.extractfile(member).read())
    destination.chmod(0o755)
