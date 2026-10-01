"""Container-side bridge for the pinned Hermes AIAgent (no Roller dependency).

Each turn restores native conversation history in the same disposable container.
The append-only journal lives separately from Hermes' compressible history.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading

REVISION = "d15ed4445207dda418b984e8bda0f68f48b8c6f3"


def plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value


def openai_compatible_tool_media(messages):
    """Move tool-result images to a following user message.

    Several OpenAI-compatible gateways accept image_url only on user messages.
    Keep Hermes' native tool result in the journal, but use this equivalent wire
    shape for the next model request.
    """
    rewritten = []
    pending_images = []

    def flush_images():
        if not pending_images:
            return
        rewritten.append({
            "role": "user",
            "content": [
                {"type": "text", "text": "Images returned by the preceding tool results:"},
                *pending_images,
            ],
        })
        pending_images.clear()

    for message in messages:
        copied = deepcopy(message)
        if copied.get("role") == "tool":
            content = copied.get("content")
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "image_url":
                        pending_images.append(deepcopy(block))
                    elif isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                        text_parts.append(block["text"])
                    elif isinstance(block, str):
                        text_parts.append(block)
                if pending_images:
                    copied["content"] = "\n".join(text_parts) or "Image attached in the following user message."
            rewritten.append(copied)
            continue
        flush_images()
        rewritten.append(copied)
    flush_images()
    return rewritten


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.tools_seen = set()
        if self.path.exists():
            # JSONL is LF-delimited; preserve Unicode line separators inside
            # JSON string values.
            for line in self.path.read_text().split("\n"):
                if not line.strip():
                    continue
                event = json.loads(line)
                message = event.get("message", {})
                if message.get("role") == "tool":
                    self.tools_seen.add(message["tool_call_id"])

    def write(self, kind, **fields):
        with self.lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": kind, "timestamp": datetime.now(timezone.utc).isoformat(), **fields}, ensure_ascii=False) + "\n")
            stream.flush()

    def observe_tools(self, messages):
        # Native flushes occur after tools and BEFORE history compression. IDs
        # survive deep copies and prevent repeats across flushes and user turns.
        with self.lock:
            for message in messages:
                call_id = message.get("tool_call_id")
                if message.get("role") != "tool" or call_id in self.tools_seen:
                    continue
                self.write("message", message={k: v for k, v in message.items() if not k.startswith("_")})
                self.tools_seen.add(call_id)


def journaled_agent(base_class, journal):
    class RecordedAgent(base_class):
        def _interruptible_api_call(self, api_kwargs):
            native_messages = api_kwargs.get("messages", [])
            journal.observe_tools(native_messages)
            outgoing = dict(api_kwargs)
            outgoing["messages"] = openai_compatible_tool_media(native_messages)
            # Capture untruncated assembled prompts and actual schemas, without
            # HTTP authorization headers or the SDK client configuration.
            journal.write("request", messages=outgoing["messages"], tools=outgoing.get("tools", []), model=outgoing.get("model"))
            response = super()._interruptible_api_call(outgoing)
            data = plain(response)
            journal.write("response", response=data)
            choices = data.get("choices", [])
            if len(choices) != 1:
                raise RuntimeError("Hermes bridge requires exactly one completion choice")
            choice = choices[0]
            journal.write("message", message=choice["message"], usage=data.get("usage") or {}, finish_reason=choice.get("finish_reason"))
            return response

        def _flush_messages_to_session_db(self, messages, conversation_history=None):
            journal.observe_tools(messages)
            return super()._flush_messages_to_session_db(messages, conversation_history)

    return RecordedAgent


def run_turn(config, state_dir):
    # Provider discovery imports optional adapters even for a custom endpoint.
    # Their lazy dependency installers must never run during a rollout.
    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    settings = {
        "security": {"allow_lazy_installs": False},
        "model": {"supports_vision": bool(config.get("vision", True))},
        "agent": {
            "image_input_mode": "native" if config.get("vision", True) else "text"
        },
    }
    if config.get("vision", True):
        # The vision tool's availability probe runs before the first model
        # request establishes Hermes' runtime-main provider. Point the probe at
        # the same disposable custom endpoint; the native fast path then keeps
        # image bytes in the main model conversation instead of describing them
        # through an unrelated auxiliary service.
        settings["auxiliary"] = {
            "vision": {
                "provider": "openai",
                "model": config["model"],
                "base_url": os.environ["OPENAI_BASE_URL"],
                "api_key": os.environ["OPENAI_API_KEY"],
            }
        }
    if config.get("context_length") is not None:
        settings["model"]["context_length"] = config["context_length"]
    (home / "config.yaml").write_text(json.dumps(settings), encoding="utf-8")
    from run_agent import AIAgent

    state_dir = Path(state_dir)
    journal = Journal(state_dir / "trace.jsonl")
    history_path = state_dir / "history.json"
    history = json.loads(history_path.read_text()) if history_path.exists() else []
    journal.write("message", message={"role": "user", "content": config["message"]})
    effort = config.get("thinking")
    reasoning = None if effort is None else ({"enabled": False} if effort == "off" else {"enabled": True, "effort": effort})
    # Keep Hermes' capability-neutral custom transport for compatibility with
    # arbitrary OpenAI-compatible models. The explicit model/image settings
    # above opt only the vision tool into its native image path.
    provider = "custom"
    from agent.auxiliary_client import reset_runtime_main, set_runtime_main
    runtime_token = set_runtime_main(
        provider,
        config["model"],
        base_url=os.environ["OPENAI_BASE_URL"],
        api_key=os.environ["OPENAI_API_KEY"],
        api_mode="chat_completions",
    )
    try:
        agent = journaled_agent(AIAgent, journal)(
            model=config["model"], provider=provider, api_mode="chat_completions",
            base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["OPENAI_API_KEY"],
            max_iterations=config["max_iterations"],
            enabled_toolsets=config.get("toolsets"),
            disabled_toolsets=None if config.get("vision", True) else ["vision"],
            quiet_mode=True, save_trajectories=False, session_id=config["session_id"],
            ephemeral_system_prompt=config["system_prompt"],
            skip_context_files=True, load_soul_identity=False, skip_memory=True,
            skip_background_review=True, checkpoints_enabled=False,
            reasoning_config=reasoning, run_budget_seconds=config["timeout_seconds"],
        )
        # This bridge captures the Chat Completions response atomically. Hermes'
        # streaming path is a separate private API and is deliberately disabled.
        agent._disable_streaming = True
        result = agent.run_conversation(
            config["message"], conversation_history=history, task_id=config["session_id"]
        )
    finally:
        reset_runtime_main(runtime_token)
    journal.observe_tools(result.get("messages", []))
    temporary = history_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result.get("messages", []), ensure_ascii=False), encoding="utf-8")
    temporary.replace(history_path)
    reply = result.get("final_response")
    if (not result.get("completed") or result.get("failed") or result.get("interrupted")
            or result.get("error") or not isinstance(reply, str) or not reply.strip()):
        raise RuntimeError("Hermes turn did not complete: " + str(result.get("error") or result.get("failure_reason") or reply or "empty response")[:2000])
    return {"ok": True, "content": reply, "api_calls": result.get("api_calls", 0)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--state-dir", default="/roller/hermes-state")
    args = parser.parse_args()
    actual = Path("/opt/hermes/roller-revision").read_text().strip()
    if actual != REVISION:
        raise RuntimeError(f"Hermes revision mismatch: expected {REVISION}, got {actual}")
    if args.check:
        from run_agent import AIAgent
        assert callable(AIAgent._interruptible_api_call)
        assert callable(AIAgent._flush_messages_to_session_db)
        print("Hermes " + REVISION)
        return
    state = Path(args.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    destination = state / "result.json"
    destination.unlink(missing_ok=True)
    try:
        result = run_turn(json.loads(Path(args.config).read_text()), state)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temporary.replace(destination)
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
