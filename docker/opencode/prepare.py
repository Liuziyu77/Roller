"""Prepare OpenCode's native plugin dependencies without making a model call."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

version, registry = sys.argv[1:]
with tempfile.TemporaryDirectory(prefix="opencode-build-") as temporary:
    root = Path(temporary)
    plugin = root / "noop.mjs"
    plugin.write_text("export default async () => ({});\n")
    config = root / "opencode.json"
    config.write_text(json.dumps({
        "plugin": [plugin.as_uri()], "model": "roller/build-check",
        "enabled_providers": ["roller"],
        "provider": {"roller": {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": "http://127.0.0.1:1/v1", "apiKey": "build-only"},
            "models": {"build-check": {"limit": {"context": 128000, "output": 8192}}},
        }},
    }))
    environment = {**os.environ, "HOME": str(root), "OPENCODE_CONFIG": str(config),
                   "OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
                   "OPENCODE_DISABLE_MODELS_FETCH": "1", "OPENCODE_DISABLE_AUTOUPDATE": "1",
                   "npm_config_registry": registry, "npm_config_fetch_retries": "1"}
    for kind in ("CONFIG", "DATA", "CACHE", "STATE"):
        environment[f"XDG_{kind}_HOME"] = str(root / kind.lower())
    # Native debug resolves tools and awaits the same dependency installer as run.
    subprocess.run(["opencode", "debug", "agent", "build"], env=environment,
                   cwd=root, stdout=subprocess.DEVNULL, check=True, timeout=300)
    source = root / "config/opencode"
    package = json.loads((source / "node_modules/@opencode-ai/plugin/package.json").read_text())
    if package["version"] != version:
        raise RuntimeError("OpenCode plugin dependency version mismatch")
    destination = Path("/opt/roller-opencode-deps")
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json", "node_modules"):
        item = source / name
        if item.is_dir():
            shutil.copytree(item, destination / name)
        else:
            shutil.copy2(item, destination / name)
