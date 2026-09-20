"""Workspace initialization and staged grading inputs."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import tempfile
from pathlib import Path, PurePosixPath

from roller.benches.base import BenchFormatError
from roller.benches.swebench_pro.constants import (
    BASELINE_REF,
    GRADE_DIR_NAME,
    OUTPUT_DIR_NAME,
    WORKSPACE_CONTAINER_PATH,
)


def _setup_command(base_commit: str, *, go_proxy: str | None = None) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", base_commit):
        raise BenchFormatError(f"invalid base_commit {base_commit!r}")
    command = f"""\
set -euo pipefail
git config --global --add safe.directory {WORKSPACE_CONTAINER_PATH}
git reset --hard {base_commit}
git clean -fd
git checkout {base_commit}
git ls-files -z > /tmp/roller-swebench-tracked
rm -rf .git
git init -q
git config user.name zqy
git config user.email zqy@localhost
cat > .git/info/exclude <<'EOF'
{OUTPUT_DIR_NAME}/
{GRADE_DIR_NAME}/
AGENTS.md
BOOTSTRAP.md
HEARTBEAT.md
IDENTITY.md
MEMORY.md
openclaw-workspace-state.json
SOUL.md
TOOLS.md
USER.md
EOF
xargs -0 -r git add -f -- < /tmp/roller-swebench-tracked
git commit -qm roller-baseline
git update-ref {BASELINE_REF} HEAD
rm -f /tmp/roller-swebench-tracked
mkdir -p {OUTPUT_DIR_NAME}
"""
    if go_proxy:
        command += f"""\
if ! command -v go >/dev/null 2>&1 && [ -x /usr/local/go/bin/go ]; then
    ln -sf /usr/local/go/bin/go /usr/local/bin/go
    if [ -x /usr/local/go/bin/gofmt ] && ! command -v gofmt >/dev/null 2>&1; then
        ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt
    fi
fi
if command -v go >/dev/null 2>&1; then
    go env -w GOPATH="$(go env GOPATH)" GOMODCACHE="$(go env GOMODCACHE)" GOPROXY={shlex.quote(go_proxy)}
fi
"""
    return command


def _default_cache_root() -> Path:
    configured = os.environ.get("SWEBENCH_PRO_CACHE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return (base / "roller" / "benches" / "swebench-pro").resolve()


def _materialize_test_patch(
    root: Path,
    cache_root: Path,
    instance_id: str,
    content: str,
) -> Path:
    packaged = root / "test_patches" / f"{instance_id}.patch"
    if packaged.is_file():
        if packaged.read_text(encoding="utf-8") != content:
            raise BenchFormatError(
                f"{instance_id}: packaged test patch does not match data"
            )
        return packaged.resolve()
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    destination = cache_root / "test_patches" / f"{digest}.patch"
    if destination.is_file():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{digest}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary_name, destination)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    return destination


def _test_fixture_patch(content: str) -> tuple[str, list[str]]:
    """Extract non-executable test data that upstream omitted from test_patch."""
    kept: list[str] = []
    paths: list[str] = []
    directories = {"testdata", "test_data", "fixtures", "__fixtures__"}
    suffixes = {".json", ".yaml", ".yml", ".txt", ".csv", ".xml", ".toml"}
    for section in re.split(r"(?=^diff --git )", content, flags=re.MULTILINE):
        if not section.startswith("diff --git "):
            continue
        try:
            header = shlex.split(section.splitlines()[0])
        except ValueError:
            continue
        if len(header) != 4 or header[2][2:] != header[3][2:]:
            continue
        path = PurePosixPath(header[3][2:])
        if (
            path.is_absolute()
            or ".." in path.parts
            or not directories.intersection(path.parts[:-1])
            or path.suffix.lower() not in suffixes
            or re.search(
                r"^(?:new file mode|new mode|old mode) 120000$",
                section,
                re.MULTILINE,
            )
            or "GIT binary patch" in section
        ):
            continue
        kept.append(section)
        paths.append(str(path))
    return "".join(kept), paths
