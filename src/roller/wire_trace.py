"""Wire-level request recorder and thinking-history compatibility proxy.

Roller points the harness's model ``baseUrl`` at a local instance of this proxy.
The proxy records what the real server would receive — full tools schema,
sampling parameters and (optionally) complete message bodies — then forwards
the request. Depending on configuration, it can also strip historical thinking
from requests or normalize equivalent Chat Completions reasoning response fields
before OpenClaw receives them. Because interception happens at the
OpenAI-compatible HTTP layer, it works for harness versions that honour
``baseUrl``.
"""

from __future__ import annotations

import json
from copy import deepcopy
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib import request as urllib_request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from roller.tool_id_identity import ToolIdRegistry


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}

BODY_KEYS = {"messages", "tools", "input"}

SENSITIVE_KEY_NAMES = {
    "authorization",
    "password",
    "secret",
    "token",
    "accesstoken",
    "refreshtoken",
    "clientsecret",
}
REDACTED_VALUE = "<redacted>"

THINKING_MESSAGE_KEYS = ("reasoning_content", "reasoning", "reasoning_text")
THINKING_BLOCK_TYPES = ("thinking", "reasoning")


def normalize_chat_completion_reasoning_response(body: Any) -> Any:
    """Normalize visible Chat Completions reasoning to ``reasoning_content``.

    OpenClaw 2026.7.1 records ``reasoning``/``reasoning_text`` response deltas as
    thinking blocks but only replays ``reasoning_content`` for a generic custom
    provider. Normalizing those equivalent response fields before OpenClaw sees
    them preserves the text in later request history without inventing content.
    """
    if not isinstance(body, dict):
        return body
    choices = body.get("choices")
    if not isinstance(choices, list):
        return body
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        for container_name in ("delta", "message"):
            message = choice.get(container_name)
            if not isinstance(message, dict):
                continue
            current = message.get("reasoning_content")
            if not isinstance(current, str) or not current:
                for source_name in ("reasoning", "reasoning_text"):
                    source = message.get(source_name)
                    if isinstance(source, str) and source:
                        message["reasoning_content"] = source
                        break
            message.pop("reasoning", None)
            message.pop("reasoning_text", None)
    return body


def strip_thinking_from_body(body: Any) -> Any:
    """Drop thinking/reasoning content from request history.

    Applied before forwarding so the model never receives prior-turn thinking;
    the current turn's reasoning streams back untouched. Handles both Chat
    Completions ``messages`` and Responses API ``input`` items.
    """
    if not isinstance(body, dict):
        return body
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for key in THINKING_MESSAGE_KEYS:
                message.pop(key, None)
            content = message.get("content")
            if isinstance(content, list):
                message["content"] = [
                    block
                    for block in content
                    if not (
                        isinstance(block, dict)
                        and block.get("type") in THINKING_BLOCK_TYPES
                    )
                ]
    responses_input = body.get("input")
    if isinstance(responses_input, list):
        body["input"] = [
            item
            for item in responses_input
            if not (isinstance(item, dict) and item.get("type") in THINKING_BLOCK_TYPES)
        ]
    return body


def _normalized_key(value: Any) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def redact_sensitive_values(value: Any) -> Any:
    """Return a JSON-compatible copy with credentials replaced by a marker."""
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            normalized = _normalized_key(key)
            if (
                normalized in SENSITIVE_KEY_NAMES
                or normalized.endswith("apikey")
                or normalized.endswith("token")
            ):
                redacted[key] = REDACTED_VALUE
            else:
                redacted[key] = redact_sensitive_values(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive_values(item) for item in value]
    return value


def redact_jsonl_file(path: Path) -> None:
    """Atomically redact credentials in a JSONL file, preserving malformed lines."""
    temporary = path.with_name(f".{path.name}.redacted.tmp")
    try:
        with path.open("r", encoding="utf-8", errors="replace") as source, temporary.open(
            "w", encoding="utf-8"
        ) as destination:
            for line in source:
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    destination.write(line)
                    continue
                destination.write(
                    json.dumps(redact_sensitive_values(parsed), ensure_ascii=False) + "\n"
                )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def request_tool_schemas(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Locate declarations without scanning arbitrary tool arguments/history."""
    groups = []
    if isinstance(body.get("tools"), list):
        groups.append({"path": "tools", "tools": body["tools"]})
    inputs = body.get("input")
    if isinstance(inputs, list):
        for index, item in enumerate(inputs):
            if isinstance(item, dict) and isinstance(item.get("additional_tools"), list):
                groups.append({"path": f"input[{index}].additional_tools", "tools": item["additional_tools"]})
    return groups


def extract_wire_tools(path: Path) -> list[Any] | None:
    """Summarize recorded tools, retaining the historical last-name-wins API.

    This summary is not a per-request schema history. The wire trace retains
    the actual declarations and their locations, including changing schemas.
    """
    merged: dict[str, Any] = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                body = entry if entry.get("kind") == "tools" else entry.get("body")
                if not isinstance(body, dict):
                    continue
                for group in request_tool_schemas(body):
                    for tool in group["tools"]:
                        if not isinstance(tool, dict):
                            continue
                        function = tool.get("function")
                        source = function if isinstance(function, dict) else tool
                        name = source.get("name")
                        if isinstance(name, str) and name:
                            merged[name] = tool
    except OSError:
        return None
    return list(merged.values()) or None


def default_responses_image_detail(body: Any, detail: str) -> Any:
    """Fill only missing image detail fields in a Responses input block list."""
    if not isinstance(body, Mapping):
        return body
    result = deepcopy(dict(body))
    for item in result.get("input", []) if isinstance(result.get("input"), list) else []:
        if not isinstance(item, dict):
            continue
        blocks = item.get("content")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "input_image" and "detail" not in block:
                block["detail"] = detail
    return result


class _TransportIO:
    """Track byte counts and failure direction without retaining payloads."""
    def __init__(self, stream, context, side):
        self.stream, self.context, self.side = stream, context, side

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def read(self, *args):
        self.context["phase"] = self.side
        value = self.stream.read(*args)
        self.context["upstream_bytes"] += len(value)
        return value

    def readline(self, *args):
        self.context["phase"] = self.side
        value = self.stream.readline(*args)
        self.context["upstream_bytes"] += len(value)
        return value

    def write(self, value):
        self.context["phase"] = self.side
        count = self.stream.write(value)
        self.context["client_bytes"] += count if isinstance(count, int) else len(value)
        return count

    def flush(self):
        self.context["phase"] = self.side
        return self.stream.flush()


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return

    def _record(self, body: bytes | None) -> None:
        proxy: WireTraceProxy = self.server.proxy  # type: ignore[attr-defined]
        if proxy.mode == "off":
            return
        entry: dict[str, Any] = {
            "seq": proxy.next_seq(),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            "kind": "request",
            "method": self.command,
            "path": self.path,
        }
        upstream_path = proxy.forward_path(self.path)
        if upstream_path != self.path:
            entry["upstream_path"] = upstream_path
        dropped_headers = sorted(
            name
            for name in self.headers
            if name.lower() in proxy.drop_request_headers
        )
        if dropped_headers:
            entry["dropped_request_headers"] = dropped_headers
        parsed: dict[str, Any] | None = None
        if body:
            try:
                candidate = json.loads(body)
                parsed = candidate if isinstance(candidate, dict) else None
            except (json.JSONDecodeError, UnicodeDecodeError):
                parsed = None
        if parsed is not None:
            entry["model"] = parsed.get("model")
            if proxy.mode == "full":
                entry["body"] = parsed
            else:
                params = {k: v for k, v in parsed.items() if k not in BODY_KEYS}
                entry["params"] = params
                messages = parsed.get("messages")
                if isinstance(messages, list):
                    entry["n_messages"] = len(messages)
                groups = request_tool_schemas(parsed)
                tools = [tool for group in groups for tool in group["tools"]]
                if tools:
                    # Capture provenance on every request, including repeated
                    # declarations. This is not a rewritten outgoing payload.
                    entry["tool_schema_sources"] = groups
                    fingerprint = json.dumps(
                        tools,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if fingerprint not in proxy.tool_schema_fingerprints:
                        proxy.tool_schema_fingerprints.add(fingerprint)
                        entry["kind"] = "tools"
                        entry["tools"] = tools
                        entry["n_tools"] = len(tools)
        proxy.write(entry)

    def _handle(self) -> None:
        proxy: WireTraceProxy = self.server.proxy  # type: ignore[attr-defined]
        started = time.monotonic()
        path = urlsplit(self.path).path.rstrip("/")
        route = next((suffix for suffix in ("/responses", "/chat/completions", "/messages", "/models")
                      if path.endswith(suffix)), "other")
        context = {"request_seq": proxy.next_seq(), "phase": "request", "upstream_bytes": 0,
                   "client_bytes": 0, "outcome": "response_complete", "response": None}
        common = {"request_seq": context["request_seq"], "method": self.command, "route": route}
        proxy.write_transport({**common, "kind": "request", "max_upstream_attempts": 1})
        output = self.wfile
        self.wfile = _TransportIO(output, context, "client")
        try:
            _ProxyHandler._forward(self, context)
        except Exception as exc:
            context["outcome"] = "client_disconnect" if context["phase"] == "client" else "upstream_stream_error"
            context["error_type"] = type(exc).__name__
            # Do not fabricate another response after headers/body were sent,
            # and never replay a request whose service-side state is unknown.
        finally:
            self.wfile = output
            if context["response"] is not None:
                try:
                    context["response"].close()
                except OSError:
                    pass
            proxy.write_transport({**common, "kind": context["outcome"],
                                   "upstream_bytes": context["upstream_bytes"],
                                   "client_bytes": context["client_bytes"],
                                   "error_type": context.get("error_type"),
                                   "status": context.get("status"), "will_retry": False,
                                   "elapsed_ms": round((time.monotonic() - started) * 1000)})

    def _forward(self, context) -> None:
        proxy: WireTraceProxy = self.server.proxy  # type: ignore[attr-defined]
        length = self.headers.get("Content-Length")
        body = self.rfile.read(int(length)) if length and length.isdigit() else None
        patch_detail = proxy.responses_image_detail and urlsplit(self.path).path.rstrip("/").endswith("/responses")
        if body and (proxy.strip_thinking or patch_detail):
            try:
                parsed = json.loads(body)
                if proxy.strip_thinking:
                    parsed = strip_thinking_from_body(parsed)
                if patch_detail:
                    parsed = default_responses_image_detail(parsed, proxy.responses_image_detail)
                body = json.dumps(parsed, ensure_ascii=False).encode("utf-8")
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        try:
            self._record(body)
        except Exception:
            pass
        self.close_connection = True
        url = proxy.upstream_origin + proxy.forward_path(self.path)
        outgoing = urllib_request.Request(url, data=body, method=self.command)
        for key, value in self.headers.items():
            normalized_key = key.lower()
            if (
                normalized_key not in HOP_BY_HOP_HEADERS
                and normalized_key != "host"
                and normalized_key not in proxy.drop_request_headers
            ):
                outgoing.add_header(key, value)
        if proxy.unique_anthropic_tool_ids:
            outgoing.add_header("Accept-Encoding", "identity")
        context["phase"] = "upstream"
        try:
            response = urllib_request.urlopen(outgoing, timeout=proxy.timeout_seconds)
        except urllib_request.HTTPError as exc:  # 4xx/5xx still carry a body
            response = exc
        except Exception as exc:
            context["outcome"] = "upstream_error"
            context["error_type"] = type(exc).__name__
            payload = json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        status = getattr(response, "status", None)
        if not isinstance(status, int):
            status = getattr(response, "code", None)
        context["status"] = status if isinstance(status, int) else None
        context["response"] = response
        response = _TransportIO(response, context, "upstream")
        context["phase"] = "client"
        self.send_response(status if isinstance(status, int) else 502)
        for key, value in response.headers.items():
            if key.lower() not in HOP_BY_HOP_HEADERS:
                self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        content_type = response.headers.get("Content-Type", "").lower()
        content_encoding = response.headers.get("Content-Encoding", "").lower()
        normalize_ids = (proxy.unique_anthropic_tool_ids
                         and self.path.split("?", 1)[0].rstrip("/").endswith("/messages")
                         and content_encoding in ("", "identity"))
        if normalize_ids and "text/event-stream" in content_type:
            while line := response.readline():
                if line.startswith(b"data:"):
                    try:
                        event = json.loads(line[5:])
                    except (ValueError, UnicodeDecodeError):
                        pass
                    else:
                        line = b"data: " + json.dumps(proxy.repair_tool_ids(event, request_seq=context["request_seq"])).encode("utf-8") + b"\n"
                self.wfile.write(line)
                self.wfile.flush()
            return
        if normalize_ids and "json" in content_type:
            payload = response.read()
            try:
                event = json.loads(payload)
            except (ValueError, UnicodeDecodeError):
                pass
            else:
                payload = json.dumps(proxy.repair_tool_ids(event, request_seq=context["request_seq"])).encode("utf-8")
            self.wfile.write(payload)
            self.wfile.flush()
            return
        normalize_reasoning = (
            proxy.normalize_reasoning_replay
            and self.path.split("?", 1)[0].rstrip("/").endswith("/chat/completions")
            and content_encoding in ("", "identity")
        )
        if normalize_reasoning and "text/event-stream" in content_type:
            while True:
                line = response.readline()
                if not line:
                    break
                self.wfile.write(self._normalize_sse_reasoning_line(line))
                self.wfile.flush()
            return
        if normalize_reasoning and "json" in content_type:
            payload = response.read()
            try:
                parsed = json.loads(payload)
                payload = json.dumps(
                    normalize_chat_completion_reasoning_response(parsed),
                    ensure_ascii=False,
                ).encode("utf-8")
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            self.wfile.write(payload)
            self.wfile.flush()
            return
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()

    @staticmethod
    def _normalize_sse_reasoning_line(line: bytes) -> bytes:
        newline = b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
        content = line[: -len(newline)] if newline else line
        if not content.startswith(b"data:"):
            return line
        data = content[5:].lstrip()
        if not data or data == b"[DONE]":
            return line
        try:
            parsed = json.loads(data)
            normalized = json.dumps(
                normalize_chat_completion_reasoning_response(parsed),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return line
        return b"data: " + normalized + newline

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle


class WireTraceProxy:
    """Reverse proxy that records wire-level model API requests to a JSONL file."""

    def __init__(
        self,
        upstream_base_url: str,
        log_path: Path,
        mode: str = "tools",
        host: str = "127.0.0.1",
        timeout_seconds: int = 600,
        strip_thinking: bool = False,
        normalize_reasoning_replay: bool = False,

        drop_query_parameters: Sequence[str] = (),
        drop_request_headers: Sequence[str] = (),
        unique_anthropic_tool_ids: bool = False,
        responses_image_detail: str | None = None,
        transport_log_path: Path | None = None,
    ) -> None:
        if mode not in ("off", "tools", "full"):
            raise ValueError(f"unknown wire trace mode: {mode}")
        if responses_image_detail not in {None, "auto", "low", "high", "original"}:
            raise ValueError("responses_image_detail must be auto|low|high|original or None")
        self.responses_image_detail = responses_image_detail
        parts = urlsplit(upstream_base_url)
        if not parts.scheme or not parts.netloc:
            raise ValueError(f"upstream base URL must be absolute: {upstream_base_url}")
        self.upstream_origin = f"{parts.scheme}://{parts.netloc}"
        self.base_path = parts.path.rstrip("/")
        self.log_path = log_path
        self.transport_log_path = transport_log_path or log_path.with_name("transport_trace.jsonl")
        self.mode = mode
        self.unique_anthropic_tool_ids = unique_anthropic_tool_ids
        self.tool_id_registry = ToolIdRegistry()
        self.strip_thinking = strip_thinking
        self.normalize_reasoning_replay = normalize_reasoning_replay
        self.drop_query_parameters = frozenset(drop_query_parameters)
        self.drop_request_headers = frozenset(
            name.strip().lower() for name in drop_request_headers if name.strip()
        )
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._seq = 0
        self.tool_schema_fingerprints: set[str] = set()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._host = host

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._server = ThreadingHTTPServer((self._host, 0), _ProxyHandler)
        self._server.daemon_threads = True
        self._server.proxy = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("proxy is not running")
        return self._server.server_address[1]

    @property
    def proxy_base_url(self) -> str:
        """The baseUrl handed to the harness; preserves the upstream path prefix."""
        return f"http://127.0.0.1:{self.port}{self.base_path}"

    def rewrite_base_url(self, current: str) -> bool:
        """True when ``current`` points at this proxy's upstream and should be rewritten."""
        return urlsplit(current).netloc == urlsplit(
            f"{self.upstream_origin}{self.base_path}"
        ).netloc and urlsplit(current).path.rstrip("/") == self.base_path

    def forward_path(self, path: str) -> str:
        """Return the upstream request target after scoped compatibility rewrites."""

        if not self.drop_query_parameters:
            return path
        parts = urlsplit(path)
        query = urlencode(
            [
                (name, value)
                for name, value in parse_qsl(parts.query, keep_blank_values=True)
                if name not in self.drop_query_parameters
            ]
        )
        return urlunsplit(("", "", parts.path, query, ""))

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def write(self, entry: Mapping[str, Any]) -> None:
        if self.mode == "off":
            return
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    def write_transport(self, entry: Mapping[str, Any]) -> None:
        # No exception strings, URL queries, body or header values. Logging
        # failure must not prevent an otherwise valid request from proceeding.
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z", **entry}
        try:
            with self._lock:
                self.transport_log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.transport_log_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def repair_tool_ids(self, event: Any, *, request_seq: int | None = None) -> Any:
        mappings: list[dict[str, str]] = []
        with self._lock:
            normalized = self.tool_id_registry.response(event, mappings)
            if mappings:
                # Identity provenance is part of the repair, not optional wire logging.
                with self.log_path.with_name("tool_id_mappings.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"request_seq": request_seq, "mappings": mappings}) + "\n")
        return normalized


def rewrite_model_config(
    config: Mapping[str, Any], proxy: WireTraceProxy
) -> dict[str, Any]:
    """Deep-copy a model config, rewriting matching provider baseUrls to the proxy."""
    rewritten = json.loads(json.dumps(config))
    providers = rewritten.get("providers")
    if isinstance(providers, dict):
        for provider in providers.values():
            if not isinstance(provider, dict):
                continue
            base_url = provider.get("baseUrl")
            if isinstance(base_url, str) and proxy.rewrite_base_url(base_url):
                provider["baseUrl"] = proxy.proxy_base_url
    return rewritten
