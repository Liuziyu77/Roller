"""Lossless response reconstruction and protocol-specific reference extraction."""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any


def anthropic_response(payload: bytes, content_type: str) -> dict[str, Any]:
    """Reassemble Messages SSE without reserializing the raw capture."""
    if "text/event-stream" not in content_type:
        message = json.loads(payload)
        if not isinstance(message, dict) or message.get("type") != "message":
            raise ValueError("response is not an Anthropic message")
        return message
    message: dict[str, Any] | None = None
    blocks: dict[int, dict[str, Any]] = {}
    inputs: dict[int, str] = {}
    stopped = False
    for frame in payload.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in frame.split("\n") if line.startswith("data:"))
        if not data:
            continue
        event = json.loads(data)
        kind = event.get("type")
        if kind == "error":
            raise ValueError("Anthropic stream returned an error")
        if kind == "message_start":
            message = event["message"]
            if not isinstance(message, dict):
                raise ValueError("invalid message_start")
            blocks = {i: b for i, b in enumerate(message.get("content", []))}
        elif kind == "content_block_start":
            blocks[event["index"]] = event["content_block"]
        elif kind == "content_block_delta":
            index = event["index"]
            block = blocks[index]
            delta = event["delta"]
            field = {"text_delta": "text", "thinking_delta": "thinking",
                     "signature_delta": "signature"}.get(delta.get("type"))
            if field:
                block[field] = block.get(field, "") + delta.get(field, "")
            elif delta.get("type") == "input_json_delta":
                inputs[index] = inputs.get(index, "") + delta.get("partial_json", "")
            else:
                raise ValueError(f"unsupported content delta: {delta.get('type')}")
        elif kind == "message_delta" and message is not None:
            message.update(event.get("delta", {}))
            message.setdefault("usage", {}).update(event.get("usage", {}))
        elif kind == "message_stop":
            stopped = True
    if message is None or not stopped:
        raise ValueError("Anthropic stream lacks message_start/message_stop")
    for index, text in inputs.items():
        blocks[index]["input"] = json.loads(text)
    message["content"] = [blocks[i] for i in sorted(blocks)]
    return message



def chat_response(payload: bytes, content_type: str) -> dict[str, Any]:
    if "text/event-stream" not in content_type:
        result = json.loads(payload)
        if not isinstance(result, dict) or len(result.get("choices", [])) != 1:
            raise ValueError("expected one Chat Completions choice")
        return result
    result = None
    message = {"role": "assistant", "content": ""}
    tools = {}
    finish = None
    done = False
    for frame in payload.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in frame.split("\n") if line.startswith("data:"))
        if not data:
            continue
        if data == "[DONE]":
            done = True
            continue
        chunk = json.loads(data)
        if chunk.get("error"):
            raise ValueError("Chat Completions stream returned an error")
        # Some gateways prefix the stream with an empty metadata frame. It is
        # not a model response and must not establish the response ID.
        if (result is None and chunk.get("id") == "" and chunk.get("choices") == []
                and not chunk.get("usage")):
            continue
        if result is None:
            result = {k: deepcopy(v) for k, v in chunk.items() if k != "choices"}
        elif chunk.get("id") and chunk["id"] != result.get("id"):
            raise ValueError("Chat Completions stream changed response ID")
        if chunk.get("usage"):
            result["usage"] = chunk["usage"]
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                raise ValueError("multiple Chat Completions choices are unsupported")
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
            for key, value in choice.get("delta", {}).items():
                if value is None:
                    continue
                if key == "tool_calls":
                    for delta in value:
                        if delta.get("type", "function") != "function" or set(delta) - {"index", "id", "type", "function"}:
                            raise ValueError("unsupported Chat Completions tool delta")
                        index = delta["index"]
                        tool = tools.setdefault(index, {"type": "function", "function": {"name": "", "arguments": ""}})
                        if "id" in delta:
                            identifier = delta["id"]
                            previous_id = tool.get("id")
                            if (not isinstance(identifier, str) or
                                    previous_id and identifier and previous_id != identifier):
                                raise ValueError("Chat Completions tool call changed ID")
                            # Some streams send an empty ID on continuation chunks.
                            if identifier or previous_id is None:
                                tool["id"] = identifier
                        if "type" in delta:
                            tool["type"] = delta["type"]
                        for field, text in delta.get("function", {}).items():
                            tool["function"][field] = tool["function"].get(field, "") + text
                elif key == "role":
                    message[key] = value
                elif isinstance(value, str):
                    message[key] = message.get(key, "") + value
                else:
                    raise ValueError(f"unsupported Chat Completions delta: {key}")
    if result is None or not done or not finish:
        raise ValueError("Chat Completions stream lacks finish_reason/[DONE]")
    if tools:
        message["tool_calls"] = [tools[i] for i in sorted(tools)]
    result["choices"] = [{"index": 0, "message": message, "finish_reason": finish}]
    return result


def responses_response(payload: bytes, content_type: str) -> dict[str, Any]:
    """Retain the terminal Responses object, including reasoning/encrypted items."""
    if "text/event-stream" not in content_type:
        result = json.loads(payload)
    else:
        result = None
        for frame in payload.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:"))
            if not data or data == "[DONE]":
                continue
            event = json.loads(data)
            if event.get("type") in {"error", "response.failed", "response.incomplete"}:
                raise ValueError("Responses stream failed or is incomplete")
            if event.get("type") == "response.completed":
                if result is not None:
                    raise ValueError("Responses stream has multiple terminal responses")
                result = event.get("response")
    if not isinstance(result, dict) or result.get("object") != "response" or not isinstance(result.get("output"), list):
        raise ValueError("Responses capture lacks a terminal response object")
    return result


def decode_response(protocol: str, payload: bytes, content_type: str) -> dict[str, Any]:
    if protocol == "anthropic-messages":
        return anthropic_response(payload, content_type)
    if protocol == "openai-chat-completions":
        return chat_response(payload, content_type)
    if protocol == "openai-responses":
        return responses_response(payload, content_type)
    raise ValueError(f"unsupported capture protocol: {protocol}")


def response_finished(protocol: str, output: dict[str, Any]) -> bool:
    if protocol == "openai-responses":
        return output.get("status") == "completed"
    if protocol == "anthropic-messages":
        return output.get("stop_reason") in {"end_turn", "tool_use", "stop_sequence"}
    choices = output.get("choices", [])
    if len(choices) != 1 or not isinstance(choices[0].get("message"), dict):
        return False
    choice = choices[0]
    if choice.get("finish_reason") == "tool_calls":
        # Some compatible gateways return this finish reason with only XML
        # text. There is no executable tool call to pair with a result.
        calls = choice["message"].get("tool_calls")
        return isinstance(calls, list) and bool(calls) and all(
            isinstance(call, dict) and isinstance(call.get("id"), str) and bool(call["id"])
            and isinstance(call.get("function"), dict) and bool(call["function"].get("name"))
            for call in calls)
    return choice.get("finish_reason") in {"stop", "function_call"}


def tool_call_ids(protocol: str, output: dict[str, Any]) -> set[str]:
    if protocol == "openai-responses":
        return {item["call_id"] for item in output.get("output", []) if isinstance(item, dict)
                and item.get("type") in {"function_call", "custom_tool_call"} and isinstance(item.get("call_id"), str)}
    if protocol == "anthropic-messages":
        return {b["id"] for b in output.get("content", []) if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("id"), str)}
    return {t["id"] for c in output.get("choices", []) for t in c.get("message", {}).get("tool_calls", []) if isinstance(t.get("id"), str)}


def tool_result_ids(protocol: str, context: dict[str, Any]) -> set[str]:
    if protocol == "openai-responses":
        return {item["call_id"] for item in context.get("input", []) if isinstance(item, dict)
                and item.get("type") in {"function_call_output", "custom_tool_call_output"} and isinstance(item.get("call_id"), str)}
    if protocol == "anthropic-messages":
        return {b["tool_use_id"] for m in context.get("messages", []) for b in (m.get("content", []) if isinstance(m.get("content"), list) else []) if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str)}
    return {m["tool_call_id"] for m in context.get("messages", []) if m.get("role") == "tool" and isinstance(m.get("tool_call_id"), str)}
