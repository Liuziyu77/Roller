"""Install a checksum-pinned official fd release for Pi's native find tool."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import platform
import tarfile
from urllib.request import urlopen


VERSION = "10.2.0"
CHECKSUMS = {
    "x86_64": "d9bfa25ec28624545c222992e1b00673b7c9ca5eb15393c40369f10b28f9c932",
    "aarch64": "4e8e596646d047d904f2c5ca74b39dccc69978b6e1fb101094e534b0b59c1bb0",
}


def main() -> None:
    architecture = platform.machine().lower()
    architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(architecture, architecture)
    if platform.system() != "Linux" or architecture not in CHECKSUMS:
        raise RuntimeError("Pi image supports Linux x86_64 and aarch64")
    stem = f"fd-v{VERSION}-{architecture}-unknown-linux-musl"
    url = f"https://github.com/sharkdp/fd/releases/download/v{VERSION}/{stem}.tar.gz"
    with urlopen(url, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != CHECKSUMS[architecture]:
        raise RuntimeError("fd archive SHA256 mismatch")
    # Copy only the expected regular binary, without extracting archive paths.
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        member = archive.getmember(f"{stem}/fd")
        if not member.isfile():
            raise RuntimeError("fd release does not contain a regular executable")
        binary = archive.extractfile(member)
        assert binary is not None
        destination = Path("/usr/local/bin/fd")
        temporary = destination.with_suffix(".tmp")
        temporary.write_bytes(binary.read())
        temporary.chmod(0o755)
        temporary.replace(destination)
        licenses = Path("/usr/local/share/licenses/roller-pi-fd")
        licenses.mkdir(parents=True, exist_ok=True)
        for name in ("LICENSE-APACHE", "LICENSE-MIT"):
            member = archive.getmember(f"{stem}/{name}")
            if not member.isfile():
                raise RuntimeError("fd release license is not a regular file")
            source = archive.extractfile(member)
            assert source is not None
            (licenses / name).write_bytes(source.read())


if __name__ == "__main__":
    main()
