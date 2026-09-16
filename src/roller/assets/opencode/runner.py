"""Container-side bridge for the pinned OpenCode CLI; standard library only."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time

VERSION = "1.18.30"
TOOLS = ("bash", "read", "write", "edit", "apply_patch", "glob", "grep")


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def atomic_text(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def native_reply(export):
    messages = export.get("messages", [])
    users = [m for m in messages if m.get("info", {}).get("role") == "user"]
    if not users:
        raise ValueError("OpenCode export has no user message")
    user_id = users[-1]["info"]["id"]
    assistants = [m for m in messages if m.get("info", {}).get("role") == "assistant" and m["info"].get("parentID") == user_id]
    if not assistants:
        raise ValueError("OpenCode turn has no assistant reply")
    last = assistants[-1]
    info = last["info"]
    if info.get("error") or not info.get("time", {}).get("completed"):
        raise ValueError("OpenCode turn did not complete: " + json.dumps(info.get("error"), ensure_ascii=False))
    if info.get("finish") not in {"stop", "end-turn"}:
        raise ValueError("OpenCode turn ended without a final response: " + str(info.get("finish")))
    content = "\n".join(p["text"] for p in last.get("parts", []) if p.get("type") == "text" and not p.get("synthetic"))
    if not content.strip():
        raise ValueError("OpenCode turn returned an empty reply")
    return content


def journal_export(path, session_id):
    messages = {}
    parts = {}
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"OpenCode journal line {line_number} is invalid JSON"
                ) from exc
            if row.get("type") == "session_export":
                exported = row.get("data")
                if not isinstance(exported, dict):
                    continue
                for message in exported.get("messages", []):
                    if not isinstance(message, dict):
                        continue
                    info = message.get("info")
                    if not isinstance(info, dict) or info.get("sessionID") != session_id:
                        continue
                    message_id = info.get("id")
                    if not isinstance(message_id, str) or not message_id:
                        continue
                    messages[message_id] = info
                    for part in message.get("parts", []):
                        if isinstance(part, dict) and isinstance(part.get("id"), str):
                            parts[part["id"]] = part
                continue
            if row.get("type") != "event":
                continue
            event = row.get("event")
            if not isinstance(event, dict):
                continue
            properties = event.get("properties")
            if not isinstance(properties, dict):
                continue
            if event.get("type") == "message.updated":
                info = properties.get("info")
                if (
                    isinstance(info, dict)
                    and info.get("sessionID") == session_id
                    and isinstance(info.get("id"), str)
                ):
                    messages[info["id"]] = info
            elif event.get("type") == "message.part.updated":
                part = properties.get("part")
                if (
                    isinstance(part, dict)
                    and part.get("sessionID", session_id) == session_id
                    and isinstance(part.get("id"), str)
                ):
                    parts[part["id"]] = part
    ordered = sorted(
        messages.values(),
        key=lambda info: (info.get("time", {}).get("created", 0), info["id"]),
    )
    return {
        "info": {"id": session_id},
        "messages": [
            {
                "info": info,
                "parts": sorted(
                    (
                        part
                        for part in parts.values()
                        if part.get("messageID") == info["id"]
                    ),
                    key=lambda part: part["id"],
                ),
            }
            for info in ordered
        ],
    }


def run_turn(config, state):
    deadline = time.monotonic() + config["timeout_seconds"]

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("OpenCode turn timed out")
        return value

    effort = config.get("thinking")
    options = {} if effort is None else {"reasoningEffort": "none" if effort == "off" else effort}
    model = config["model"]
    native_config = {
        "$schema": "https://opencode.ai/config.json",
        "model": "roller/" + model,
        "enabled_providers": ["roller"],
        "provider": {"roller": {
            "npm": "@ai-sdk/openai-compatible", "name": "Roller API",
            "options": {"baseURL": os.environ["OPENAI_BASE_URL"], "apiKey": "{env:OPENAI_API_KEY}", "timeout": max(1, int(remaining() * 1000))},
            "models": {model: {
                "id": model, "name": model, "tool_call": True, "reasoning": True,
                "interleaved": {"field": "reasoning_content"},
                "limit": {"context": config["context_length"], "output": config["output_tokens"]},
                "modalities": {"input": ["text"], "output": ["text"]}, "options": options,
            }},
        }},
        "plugin": ["file:///roller/opencode_journal.mjs"],
        "share": "disabled", "autoupdate": False, "snapshot": False,
        "compaction": {"auto": False, "prune": False},
        "lsp": False, "formatter": False,
        "permission": {"*": "deny", **{name: "allow" for name in TOOLS}, "external_directory": "allow"},
        "agent": {"build": {"steps": config["max_steps"]}, "title": {"disable": True}, "summary": {"disable": True}},
    }
    atomic_json(state / "opencode.json", native_config)
    session_file = state / "session.json"
    session = json.loads(session_file.read_text()) if session_file.exists() else {}
    args = ["opencode", "run", "--format", "json", "--thinking", "--agent", "build", "--model", "roller/" + model, "--title", "Roller task"]
    if session.get("id"):
        args += ["--session", session["id"]]
    stream_path = state / "cli.jsonl"
    with stream_path.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=output, stderr=None, text=True)
        try:
            process.communicate(config["message"], timeout=remaining())
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            raise TimeoutError("OpenCode CLI timed out") from None
    events = []
    for line in stream_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # CLI diagnostics are separate from the authoritative journal.
        if isinstance(row, dict):
            events.append(row)
    ids = {e["sessionID"] for e in events if isinstance(e.get("sessionID"), str)}
    if len(ids) != 1 or (session.get("id") and ids != {session["id"]}):
        raise ValueError("OpenCode CLI did not return exactly the expected session")
    session_id = ids.pop()
    atomic_json(session_file, {"id": session_id})
    errors = [e.get("error") for e in events if e.get("type") == "error"]
    if process.returncode or errors:
        raise RuntimeError(
            "OpenCode CLI failed: " + json.dumps(errors, ensure_ascii=False)[:2000]
        )
    exported = subprocess.run(["opencode", "export", session_id], capture_output=True, text=True, timeout=remaining())
    raw_export_path = state / "session-export.raw.json"
    atomic_text(raw_export_path, exported.stdout)
    if exported.returncode:
        raise RuntimeError("OpenCode session export failed: " + exported.stderr[-1000:])
    try:
        export = json.loads(exported.stdout)
    except json.JSONDecodeError as exc:
        try:
            export = journal_export(state / "trace.jsonl", session_id)
            content = native_reply(export)
        except (OSError, TypeError, ValueError, KeyError) as recovery_error:
            raise ValueError(
                f"OpenCode export JSON is invalid ({exc}); "
                f"journal recovery failed: {recovery_error}"
            ) from recovery_error
        with (state / "trace.jsonl").open("a", encoding="utf-8") as journal:
            journal.write(
                json.dumps(
                    {
                        "type": "export_recovery",
                        "session_id": session_id,
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        return {
            "ok": True,
            "content": content,
            "session_id": session_id,
            "export_recovered": True,
        }
    if not isinstance(export, dict):
        raise ValueError("OpenCode export must be a JSON object")
    if export.get("info", {}).get("id") != session_id:
        raise ValueError("OpenCode exported a different session")
    atomic_json(state / "session-export.json", export)
    with (state / "trace.jsonl").open("a", encoding="utf-8") as journal:
        journal.write(json.dumps({"type": "session_export", "data": export}, ensure_ascii=False) + "\n")
    return {"ok": True, "content": native_reply(export), "session_id": session_id}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--state-dir", default="/roller/opencode-state")
    args = parser.parse_args()
    if args.check:
        version = subprocess.check_output(["opencode", "--version"], text=True, timeout=30).strip()
        if version != VERSION:
            raise ValueError(f"OpenCode version mismatch: expected {VERSION}, got {version}")
        print(version)
        return
    state = Path(args.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    destination = state / "result.json"
    destination.unlink(missing_ok=True)
    try:
        result = run_turn(json.loads(Path(args.config).read_text()), state)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    atomic_json(destination, result)
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
