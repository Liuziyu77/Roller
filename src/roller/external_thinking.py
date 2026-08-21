"""Explicit tool-based reasoning for OpenAI Responses rollouts.

The protocol mirrors claw-eval's ``external`` and ``external_always`` modes:
native provider reasoning is disabled, detailed work is emitted as arguments
to a local ``deep_think`` tool, and the tool result is a small placeholder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


EXTERNAL_THINKING_MODES = ("off", "external", "external_always")
DEEP_THINK_TOOL_NAME = "deep_think"
DEEP_THINK_RESULT = "------"

DEEP_THINK_TOOL = {
    "type": "function",
    "name": DEEP_THINK_TOOL_NAME,
    "description": (
        "Record detailed reasoning and intermediate work. Follow the injected "
        "reasoning policy for when to call this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "thoughts": {
                "type": "string",
                "description": "Detailed reasoning or intermediate work.",
            }
        },
        "required": ["thoughts"],
        "additionalProperties": False,
    },
    "strict": True,
}

EXTERNAL_THINKING_INSTRUCTIONS = """Solve the user's task carefully.

# Reasoning
The deep_think tool is your scratchpad and is where your reasoning happens:
whatever you do not write there, you have not worked out.

Required calls:
- You MUST call deep_think before the first action of the turn.
- You MUST call it again before any step that is expensive to undo, including
  a state-changing or destructive tool call, and before the final answer.

In deep_think, restate what is actually being asked and the given constraints.
Split the task into ordered sub-problems and solve each explicitly instead of
jumping to the conclusion. When a step splits into cases, enumerate and resolve
each case. Then check the work: verify each claim against the constraints, test
one boundary or degenerate case, and look specifically for the error you would
most plausibly have made. If a check fails, redo that step rather than patching
the conclusion.

Outside the required calls above, call deep_think again only for materially new
state: a tool result that changes the plan, a failed check, or a sub-problem you
had not opened. Never use it to narrate progress or restate reasoning already
recorded. After it returns, use the other available tools, call deep_think again
under these rules, or give the final answer. Do not repeat the deep_think tool
transcript in the final answer."""

EXTERNAL_ALWAYS_THINK_INSTRUCTIONS = """Solve the user's task carefully.

# THINK phase
This is the reasoning phase of one logical agent turn. You MUST call
deep_think exactly once. Do not call any other tool and do not produce
user-facing text in this phase. The other tool definitions are present only so
you can plan against their exact capabilities and argument schemas.
Reason only about the user's task, concrete evidence, and tool choices. NEVER
mention skills, whether a skill applies, prompt instructions, or the THINK/ACT
protocol in the thoughts argument.

Put all reasoning needed for the immediately following action in the
deep_think thoughts argument. On the first turn, identify the task, constraints,
and an ordered plan. On later turns, start from the newest tool results or user
message, state what changed, and update the plan instead of repeating the whole
problem. Decide either:
- the smallest sufficient batch of business-tool calls, explaining the purpose
  of every call in a parallel batch; or
- that the task is ready for a final answer, after checking the answer against
  the user's constraints and the available evidence.

Include calculations, case analysis, and error checks when they affect the next
action. Preserve every exact ID, value, path, and argument the ACT phase will
need. Do not use deep_think merely to narrate progress, and do not discuss skill
availability, prompt rules, or protocol mechanics. Do not assume a tool
succeeded before seeing its result."""

EXTERNAL_ALWAYS_ACT_INSTRUCTIONS = """Solve the user's task carefully.

# ACT phase
The immediately preceding deep_think call contains the reasoning for this
logical turn. Act on it now: either call the available business tools (parallel
calls are allowed when the thought planned a batch) or provide the user-facing
answer. Do not repeat or expose the scratchpad, and do not return an empty
response. The deep_think tool is intentionally unavailable in this phase."""


def normalize_external_thinking_mode(value: str | None) -> str:
    normalized = (value or "off").strip().lower().replace("-", "_")
    if normalized not in EXTERNAL_THINKING_MODES:
        choices = "|".join(EXTERNAL_THINKING_MODES)
        raise ValueError(f"external thinking mode must be {choices}, got {value!r}")
    return normalized


def external_thinking_mode_from_metadata(
    metadata: Mapping[str, Any] | None,
) -> str:
    if not isinstance(metadata, Mapping):
        return "off"
    value = metadata.get("external_thinking")
    if isinstance(value, Mapping):
        value = value.get("mode")
    if value is None:
        provenance = metadata.get("provenance")
        if isinstance(provenance, Mapping):
            value = provenance.get("external_thinking")
    if value is not None and not isinstance(value, str):
        raise ValueError("external thinking metadata mode must be a string")
    return normalize_external_thinking_mode(value)


def validate_external_thinking_provider(
    config: Mapping[str, Any] | None,
    model_name: str,
    mode: str,
) -> None:
    normalized = normalize_external_thinking_mode(mode)
    if normalized == "off":
        return
    if config is None:
        raise ValueError("external thinking requires --model-config")
    if "/" not in model_name:
        raise ValueError(
            "external thinking model must use a provider/model reference"
        )
    provider_name = model_name.split("/", 1)[0]
    providers = config.get("providers")
    provider = providers.get(provider_name) if isinstance(providers, Mapping) else None
    if not isinstance(provider, Mapping):
        raise ValueError(
            f"external thinking provider {provider_name!r} is missing from model config"
        )
    if provider.get("api") != "openai-responses":
        raise ValueError(
            "external thinking requires the selected model provider to use "
            "api='openai-responses'"
        )
    base_url = provider.get("baseUrl")
    parsed = urlsplit(base_url) if isinstance(base_url, str) else None
    if parsed is None or not parsed.scheme or not parsed.netloc:
        raise ValueError(
            f"external thinking requires an absolute baseUrl for provider {provider_name!r}"
        )


def _tool_name(tool: Any) -> str | None:
    if not isinstance(tool, dict):
        return None
    name = tool.get("name")
    if isinstance(name, str):
        return name
    function = tool.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    return None


def _input_function_calls(body: dict[str, Any]) -> list[tuple[int, str]]:
    calls: list[tuple[int, str]] = []
    items = body.get("input")
    if not isinstance(items, list):
        return calls
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        name = item.get("name")
        if isinstance(name, str):
            calls.append((index, name))
    return calls


def _last_user_index(body: dict[str, Any]) -> int:
    items = body.get("input")
    if not isinstance(items, list):
        return -1
    return max(
        (
            index
            for index, item in enumerate(items)
            if isinstance(item, dict) and item.get("role") == "user"
        ),
        default=-1,
    )


class ExternalThinkingController:
    """Rewrite one rollout's Responses requests to enforce external CoT."""

    def __init__(self, mode: str) -> None:
        self.mode = normalize_external_thinking_mode(mode)

    @staticmethod
    def _disable_native_reasoning(body: dict[str, Any]) -> None:
        reasoning = body.get("reasoning")
        normalized = dict(reasoning) if isinstance(reasoning, dict) else {}
        normalized["effort"] = "none"
        body["reasoning"] = normalized

    @staticmethod
    def _require_deep_think_tool(body: dict[str, Any]) -> list[dict[str, Any]]:
        tools = body.get("tools")
        if not isinstance(tools, list):
            raise ValueError("external thinking requires a Responses tools array")
        if not any(_tool_name(tool) == DEEP_THINK_TOOL_NAME for tool in tools):
            raise ValueError(
                "external thinking plugin did not expose the 'deep_think' tool"
            )
        normalized = [
            dict(DEEP_THINK_TOOL)
            if _tool_name(tool) == DEEP_THINK_TOOL_NAME
            else tool
            for tool in tools
            if isinstance(tool, dict)
        ]
        body["tools"] = normalized
        return normalized

    @staticmethod
    def _phase_for_external_always(body: dict[str, Any]) -> str:
        calls = _input_function_calls(body)
        if not calls:
            return "think"
        last_call_index, last_call_name = calls[-1]
        if _last_user_index(body) > last_call_index:
            return "think"
        return "act" if last_call_name == DEEP_THINK_TOOL_NAME else "think"

    def rewrite_request(self, body: Any) -> tuple[Any, str | None]:
        if self.mode == "off":
            return body, None
        if not isinstance(body, dict) or not isinstance(body.get("input"), list):
            raise ValueError("external thinking requires an OpenAI Responses request")

        tools = self._require_deep_think_tool(body)
        self._disable_native_reasoning(body)
        body["store"] = False

        if self.mode == "external":
            already_thought = any(
                name == DEEP_THINK_TOOL_NAME
                for _index, name in _input_function_calls(body)
            )
            body["instructions"] = EXTERNAL_THINKING_INSTRUCTIONS
            body["parallel_tool_calls"] = already_thought
            if already_thought:
                body.pop("tool_choice", None)
                return body, "optional"
            body["tool_choice"] = {
                "type": "function",
                "name": DEEP_THINK_TOOL_NAME,
            }
            return body, "think"

        phase = self._phase_for_external_always(body)
        if phase == "think":
            body["instructions"] = EXTERNAL_ALWAYS_THINK_INSTRUCTIONS
            body["parallel_tool_calls"] = False
            body["tool_choice"] = {
                "type": "function",
                "name": DEEP_THINK_TOOL_NAME,
            }
        else:
            body["instructions"] = EXTERNAL_ALWAYS_ACT_INSTRUCTIONS
            body["tools"] = [
                tool for tool in tools if _tool_name(tool) != DEEP_THINK_TOOL_NAME
            ]
            body["parallel_tool_calls"] = True
            body.pop("tool_choice", None)
        return body, phase


@dataclass(frozen=True)
class ExternalThinkingTraceStats:
    mode: str
    deep_think_calls: int
    thought_action_pairs: int


def _assistant_messages(trace_path: Path) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    with trace_path.open("r", encoding="utf-8", errors="replace") as source:
        for line in source:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = event.get("message") if isinstance(event, dict) else None
            if isinstance(message, dict) and message.get("role") == "assistant":
                messages.append(message)
    return messages


def _assistant_parts(
    message: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    text: list[str] = []
    calls: list[dict[str, Any]] = []
    content = message.get("content")
    if not isinstance(content, list):
        return text, calls
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            if block["text"].strip():
                text.append(block["text"])
        elif block.get("type") == "toolCall":
            calls.append(block)
    return text, calls


def _validate_deep_think_call(call: dict[str, Any]) -> None:
    arguments = call.get("arguments")
    thoughts = arguments.get("thoughts") if isinstance(arguments, dict) else None
    if not isinstance(thoughts, str) or not thoughts.strip():
        raise ValueError("deep_think requires a non-empty string 'thoughts' argument")


def validate_external_thinking_trace(
    trace_path: Path, mode: str
) -> ExternalThinkingTraceStats:
    """Validate accepted raw assistant messages after an external-CoT run."""
    normalized = normalize_external_thinking_mode(mode)
    if normalized == "off":
        return ExternalThinkingTraceStats(normalized, 0, 0)
    messages = _assistant_messages(trace_path)
    if not messages:
        raise ValueError("external thinking trace contains no assistant messages")

    deep_think_calls = 0
    if normalized == "external":
        _text, first_calls = _assistant_parts(messages[0])
        first_thinks = [
            call for call in first_calls if call.get("name") == DEEP_THINK_TOOL_NAME
        ]
        if len(first_thinks) != 1:
            raise ValueError("external mode first response must call deep_think exactly once")
        for message in messages:
            _text, calls = _assistant_parts(message)
            for call in calls:
                if call.get("name") == DEEP_THINK_TOOL_NAME:
                    _validate_deep_think_call(call)
                    deep_think_calls += 1
        return ExternalThinkingTraceStats(normalized, deep_think_calls, 0)

    if len(messages) % 2:
        raise ValueError("external_always trace ended with an unpaired THINK response")
    for index, message in enumerate(messages):
        text, calls = _assistant_parts(message)
        if index % 2 == 0:
            if text:
                raise ValueError("external_always THINK response produced user-facing text")
            if len(calls) != 1 or calls[0].get("name") != DEEP_THINK_TOOL_NAME:
                raise ValueError(
                    "external_always THINK response must call deep_think exactly once"
                )
            _validate_deep_think_call(calls[0])
            deep_think_calls += 1
        elif any(call.get("name") == DEEP_THINK_TOOL_NAME for call in calls):
            raise ValueError("external_always ACT response must not call deep_think")
        elif not text and not calls:
            raise ValueError(
                "external_always ACT response returned neither a tool call nor text"
            )
    return ExternalThinkingTraceStats(
        normalized,
        deep_think_calls,
        len(messages) // 2,
    )
