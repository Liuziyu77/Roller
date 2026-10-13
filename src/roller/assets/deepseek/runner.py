"""Container-only bridge to the pinned official DeepSeek Harness SDK.

A private Unix socket keeps one SDK/runtime process (and its Bash PTY) alive
across Roller turns. The host owns the hard deadline and container cleanup.
"""
from __future__ import annotations

import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

VERSION = "0.1.5rc1"
STATE = Path("/roller/deepseek-state")
SOCKET = str(STATE / "runner.sock")


def check():
    from deepseek_harness_runtime import bundled_runtime_path
    for package in ("deepseek-harness-sdk", "deepseek-harness-runtime-bin"):
        installed = version(package)
        if installed != VERSION:
            raise RuntimeError(f"{package} version mismatch: expected {VERSION}, got {installed}")
    if not os.access(bundled_runtime_path(), os.X_OK):
        raise RuntimeError("DeepSeek native runtime is not executable")
    return VERSION


def redact(value):
    text = json.dumps(value, ensure_ascii=False)
    key = os.environ.get("DEEPSEEK_API_KEY")
    return text.replace(key, "[REDACTED]") if key else text


def journal(value):
    with (STATE / "trace.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(redact(value) + "\n")


def write_result(value):
    temp = STATE / "result.json.tmp"
    temp.write_text(redact(value), encoding="utf-8")
    temp.replace(STATE / "result.json")


def serve(config):
    from deepseek_harness import DeepSeekHarness
    check()
    os.environ["DSH_SYSTEM_PROMPT"] = config["system_prompt"]
    os.environ["DSH_CONTEXT_WINDOW"] = str(config["context_length"])
    os.environ["ROLLER_DEEPSEEK_MODEL"] = config["model"]
    os.environ["ROLLER_DEEPSEEK_OUTPUT_TOKENS"] = str(config["output_tokens"])
    journal({"type": "metadata", "schema": "roller.deepseek-events/v1", "version": VERSION,
             "profile": config.get("profile", "sdk"), "session_id": config["session_id"], "model": config["model"]})
    # Full sdk builds its native prompt; append the task contract rather than
    # replacing native tool instructions. Minimal reads DSH_SYSTEM_PROMPT itself.
    patches = [str(STATE / "roller.patch.yml")]
    if config.get("profile", "sdk") != "sdk-minimal":
        prompt_patch = STATE / "task-prompt.patch.yml"
        prompt_patch.write_text(json.dumps([{"id": "system-prompt", "config": {
            "personaSuffix": "Your working directory is {{cwd}}.\n\n" + config["system_prompt"]
        }}]), encoding="utf-8")
        patches.append(str(prompt_patch))
    # Keep initialization errors reviewable even when there is no native turn.
    with DeepSeekHarness(
        model=config["model"], provider="deepseek-official", cwd=os.getcwd(),
        profile=config.get("profile", "sdk"), dsh_home="/roller/deepseek-home",
        patches=tuple(patches),
        reasoning_effort=config["thinking"], max_tokens=config["output_tokens"],
        initialize_timeout_seconds=min(60, config["timeout_seconds"]),
    ) as harness:
        session = harness.start_session(config["session_id"])
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(SOCKET)
            os.chmod(SOCKET, 0o600)
            server.listen(1)
            while True:
                connection, _ = server.accept()
                with connection, connection.makefile("r", encoding="utf-8") as stream:
                    payload = json.loads(stream.readline())
                    try:
                        result = session.run(payload["message"], on_notification=lambda n: journal(
                            {"type": "notification", "method": n.method, "payload": n.payload}))
                        limit_path = STATE / "limit.json"
                        if limit_path.exists():
                            limit = json.loads(limit_path.read_text())
                            if limit["turn"] == payload["turn"]:
                                raise RuntimeError(limit["error"])
                        if result.finish_reason != "completed" or not result.final_response.strip():
                            reason = next((e["data"]["reason"] for e in reversed(result.events)
                                           if e.get("type") == "turn/end"), result.finish_reason)
                            raise RuntimeError(f"DeepSeek turn did not complete: {json.dumps(reason, ensure_ascii=False)}")
                        value = {"ok": True, "content": result.final_response}
                    except Exception as exc:
                        value = {"ok": False, "error": str(exc)}
                        journal({"type": "runner_error", "error": str(exc)})
                    connection.sendall((redact(value) + "\n").encode())
                    if not value["ok"]:
                        return


def send(config):
    process = None
    if not Path(SOCKET).exists():
        with (STATE / "runtime.log").open("a") as log:
            process = subprocess.Popen([sys.executable, __file__, "--serve", str(STATE / "turn.json")],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=True)
    deadline = time.monotonic() + config["timeout_seconds"]
    with socket.socket(socket.AF_UNIX) as client:
        while True:
            if process is not None and process.poll() is not None:
                raise RuntimeError("DeepSeek runtime startup failed; see the native journal and rollout log")
            try:
                client.connect(SOCKET)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("DeepSeek runtime startup timed out")
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
    parser.add_argument("--serve", type=Path)
    args = parser.parse_args()
    try:
        if args.check:
            print(check())
            return 0
        config = json.loads((args.serve or args.config).read_text())
        if args.serve:
            serve(config)
            return 0
        return send(config)
    except Exception as exc:
        if not args.check:
            journal({"type": "runner_error", "error": str(exc)})
            write_result({"ok": False, "error": str(exc), "timeout": isinstance(exc, TimeoutError)})
        print(redact({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
