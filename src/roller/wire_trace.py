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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib import request as urllib_request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from roller.response_metadata import ResponseMetadata

from roller.codex_tools import tool_snapshot
from roller.capture import CallRecorder


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


def extract_wire_tools(path: Path) -> list[Any] | None:
    """Merge every tools schema recorded across wire-trace requests."""
    try:
        # JSONL records are separated by LF.  Unicode line separators can
        # legally occur inside string values and must not split a record.
        lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return None
    merged: dict[str, Any] = {}
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") == "tools":
            tools = entry.get("tools")
        else:
            body = entry.get("body")
            tools = body.get("tools") if isinstance(body, dict) else None
        if not isinstance(tools, list):
            continue
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            function = tool.get("function")
            source = function if isinstance(function, dict) else tool
            name = source.get("name")
            if isinstance(name, str) and name:
                merged[name] = tool
    return list(merged.values()) or None


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return

    def _record(self, body: bytes | None) -> int:
        proxy: WireTraceProxy = self.server.proxy  # type: ignore[attr-defined]
        capture_responses_tools = (
            proxy.responses_tools_path is not None
            and self.command == "POST"
            and urlsplit(self.path).path.rstrip("/").endswith("/responses")
        )
        entry: dict[str, Any] = {
            "seq": proxy.next_seq(responses_request=capture_responses_tools),
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
        if capture_responses_tools and parsed is None:
            raise ValueError("Cannot capture tools from a non-object Responses request")
        if parsed is not None:
            entry["model"] = parsed.get("model")
            if capture_responses_tools:
                proxy.write_tools({
                    "kind": "codex_tools", "seq": entry["seq"],
                    "snapshot": tool_snapshot(parsed),
                })
            if proxy.mode == "full":
                entry["body"] = parsed
            else:
                params = {k: v for k, v in parsed.items() if k not in BODY_KEYS}
                entry["params"] = params
                messages = parsed.get("messages")
                if isinstance(messages, list):
                    entry["n_messages"] = len(messages)
                tools = parsed.get("tools")
                if isinstance(tools, list) and tools:
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
        if (proxy.capture_recorder is not None and parsed is not None
                and self.command == "POST"
                and urlsplit(self.path).path.rstrip("/").endswith(("/messages", "/chat/completions"))):
            native_session = self.headers.get(proxy.capture_identity_header) if proxy.capture_identity_header else None
            proxy.capture_recorder.begin(entry["seq"], proxy.forward_path(self.path), parsed,
                native_identity={"session_id": native_session} if native_session else None)
        return entry["seq"]

    def _handle(self) -> None:
        proxy: WireTraceProxy = self.server.proxy  # type: ignore[attr-defined]
        length = self.headers.get("Content-Length")
        body = self.rfile.read(int(length)) if length and length.isdigit() else None
        if body and proxy.strip_thinking:
            try:
                parsed = strip_thinking_from_body(json.loads(body))
                body = json.dumps(parsed, ensure_ascii=False).encode("utf-8")
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        request_seq = None
        try:
            request_seq = self._record(body)
        except Exception as exc:
            if proxy.responses_tools_path is not None:
                proxy.recording_error = str(exc)
            if proxy.capture_recorder is not None:
                proxy.capture_recorder.error = str(exc)
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
        try:
            response = urllib_request.urlopen(outgoing, timeout=proxy.timeout_seconds)
        except urllib_request.HTTPError as exc:  # 4xx/5xx still carry a body
            response = exc
        except Exception as exc:
            if proxy.capture_recorder is not None and request_seq in proxy.capture_recorder.request_seqs:
                proxy.capture_recorder.finish(request_seq, status=None, content_type="",
                    content_encoding="", complete=False, error=f"upstream transport error: {type(exc).__name__}")
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
        self.send_response(status if isinstance(status, int) else 502)
        for key, value in response.headers.items():
            if key.lower() not in HOP_BY_HOP_HEADERS:
                self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        content_type = response.headers.get("Content-Type", "").lower()
        content_encoding = response.headers.get("Content-Encoding", "").lower()
        observer = ResponseMetadata(content_type, content_encoding)
        recorder = proxy.capture_recorder
        captured = recorder is not None and request_seq in recorder.request_seqs
        response_file = None
        capture_error = None
        complete = False
        if captured:
            try:
                response_file = recorder.open_response(request_seq)
            except Exception as exc:
                capture_error = recorder.error = str(exc)

        def observe(chunk: bytes) -> None:
            nonlocal capture_error
            observer.feed(chunk)
            if response_file is not None:
                try:
                    recorder.append_response(response_file, chunk)
                except Exception as exc:
                    capture_error = recorder.error = str(exc)

        def record_response() -> None:
            # Diagnostics must never prevent delivery of the original response.
            if isinstance(request_seq, int):
                try:
                    proxy.write({"kind": "response_metadata", "request_seq": request_seq,
                                 "http_status": status, **observer.finish()})
                except Exception:
                    pass
        normalize_reasoning = (
            proxy.normalize_reasoning_replay
            and self.path.split("?", 1)[0].rstrip("/").endswith("/chat/completions")
            and content_encoding in ("", "identity")
        )
        try:
            if normalize_reasoning and "text/event-stream" in content_type:
                while True:
                    line = response.readline()
                    if not line:
                        break
                    observe(line)
                    self.wfile.write(self._normalize_sse_reasoning_line(line))
                    self.wfile.flush()
            elif normalize_reasoning and "json" in content_type:
                payload = response.read()
                observe(payload)
                try:
                    payload = json.dumps(normalize_chat_completion_reasoning_response(
                        json.loads(payload)), ensure_ascii=False).encode("utf-8")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
                self.wfile.write(payload)
                self.wfile.flush()
            else:
                while True:
                    # read1 forwards available SSE bytes without waiting for a
                    # 64 KiB buffer or the end of the upstream response.
                    chunk = response.read1(65536) if captured and hasattr(response, "read1") else response.read(65536)
                    if not chunk:
                        break
                    observe(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()
            complete = True
        except Exception as exc:
            capture_error = f"response forwarding interrupted: {type(exc).__name__}"
            raise
        finally:
            if response_file is not None:
                try:
                    response_file.close()
                except Exception as exc:
                    capture_error = recorder.error = str(exc)
            if captured:
                recorder.finish(request_seq, status=status, content_type=content_type,
                    content_encoding=content_encoding, complete=complete and not capture_error,
                    error=capture_error)
            record_response()
            close_response = getattr(response, "close", None)
            if callable(close_response):
                close_response()

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
        responses_tools_path: Path | None = None,
        capture_recorder: CallRecorder | None = None,
        capture_identity_header: str | None = None,
    ) -> None:
        if mode not in ("tools", "full"):
            raise ValueError(f"unknown wire trace mode: {mode}")
        parts = urlsplit(upstream_base_url)
        if not parts.scheme or not parts.netloc:
            raise ValueError(f"upstream base URL must be absolute: {upstream_base_url}")
        self.upstream_origin = f"{parts.scheme}://{parts.netloc}"
        self.base_path = parts.path.rstrip("/")
        self.log_path = log_path
        self.mode = mode
        self.responses_tools_path = responses_tools_path
        self.capture_recorder = capture_recorder
        self.capture_identity_header = capture_identity_header
        self.recording_error: str | None = None
        self.strip_thinking = strip_thinking
        self.normalize_reasoning_replay = normalize_reasoning_replay
        self.drop_query_parameters = frozenset(drop_query_parameters)
        self.drop_request_headers = frozenset(
            name.strip().lower() for name in drop_request_headers if name.strip()
        )
        if capture_identity_header:
            self.drop_request_headers |= {capture_identity_header.lower()}
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._seq = 0
        self.responses_request_count = 0
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

    def next_seq(self, *, responses_request: bool = False) -> int:
        with self._lock:
            self._seq += 1
            if responses_request:
                self.responses_request_count += 1
            return self._seq

    def write_tools(self, entry: Mapping[str, Any]) -> None:
        assert self.responses_tools_path is not None
        with self._lock, self.responses_tools_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def write(self, entry: Mapping[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")


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
