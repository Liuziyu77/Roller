"""Container-only bridge to the pinned official Pi Harness SDK.

A private Unix socket keeps one native AgentSession process alive
across Roller turns. The host owns the hard deadline and container cleanup.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

STATE = Path("/roller/pi-state")
SOCKET = str(STATE / "runner.sock")


def check():
    result = subprocess.run(["node", "/roller/pi_sdk.mjs", "--check"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Pi SDK check failed: " + (result.stderr or result.stdout).strip()[-2000:])
    return result.stdout.strip()


def redact(value):
    text = json.dumps(value, ensure_ascii=False)
    key = os.environ.get("PI_API_KEY")
    return text.replace(key, "[REDACTED]") if key else text


def journal(value):
    with (STATE / "trace.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(redact(value) + "\n")


def write_result(value):
    temp = STATE / "result.json.tmp"
    temp.write_text(redact(value), encoding="utf-8")
    temp.replace(STATE / "result.json")


def send(config):
    process = None
    if not Path(SOCKET).exists():
        with (STATE / "runtime.log").open("a") as log:
            process = subprocess.Popen(["node", "/roller/pi_sdk.mjs", "--serve", str(STATE / "turn.json")],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=True)
    deadline = time.monotonic() + config["timeout_seconds"]
    with socket.socket(socket.AF_UNIX) as client:
        while True:
            if process is not None and process.poll() is not None:
                raise RuntimeError("Pi runtime startup failed; see the native journal and rollout log")
            try:
                client.connect(SOCKET)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Pi runtime startup timed out")
                time.sleep(0.05)
        client.settimeout(max(0.001, deadline - time.monotonic()))
        client.sendall((json.dumps(config) + "\n").encode())
        with client.makefile("r", encoding="utf-8") as stream:
            value = json.loads(stream.readline())
        write_result(value)
        return 0 if value["ok"] else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    try:
        if args.check:
            print(check())
            return 0
        config = json.loads(args.config.read_text())
        return send(config)
    except Exception as exc:
        if not args.check:
            journal({"type": "runner_error", "error": str(exc)})
            write_result({"ok": False, "error": str(exc), "timeout": isinstance(exc, TimeoutError)})
        print(redact({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
