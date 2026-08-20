"""Wire-level request recorder: a transparent reverse proxy in front of the model API.

Roller points the harness's model ``baseUrl`` at a local instance of this proxy.
The proxy records what the real server would receive — full tools schema,
sampling parameters and (optionally) complete message bodies — then forwards
the request unchanged. Because the interception happens at the OpenAI-compatible
HTTP layer, it works for any harness version that honours ``baseUrl``.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib import request as urllib_request
from urllib.parse import urlsplit

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
    """Return the tools schema recorded in a wire trace file, if any."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
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
        if isinstance(tools, list) and tools:
            return tools
    return None


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return

    def _record(self, body: bytes | None) -> None:
        proxy: WireTraceProxy = self.server.proxy  # type: ignore[attr-defined]
        entry: dict[str, Any] = {
            "seq": proxy.next_seq(),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            "kind": "request",
            "method": self.command,
            "path": self.path,
        }
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
                tools = parsed.get("tools")
                if isinstance(tools, list) and not proxy.tools_logged and tools:
                    proxy.tools_logged = True
                    entry["kind"] = "tools"
                    entry["tools"] = tools
                    entry["n_tools"] = len(tools)
        proxy.write(entry)

    def _handle(self) -> None:
        proxy: WireTraceProxy = self.server.proxy  # type: ignore[attr-defined]
        length = self.headers.get("Content-Length")
        body = self.rfile.read(int(length)) if length and length.isdigit() else None
        if body and proxy.strip_thinking:
            try:
                parsed = json.loads(body)
                parsed = strip_thinking_from_body(parsed)
                body = json.dumps(parsed, ensure_ascii=False).encode("utf-8")
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        try:
            self._record(body)
        except Exception:
            pass
        self.close_connection = True
        url = proxy.upstream_origin + self.path
        outgoing = urllib_request.Request(url, data=body, method=self.command)
        for key, value in self.headers.items():
            if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host":
                outgoing.add_header(key, value)
        try:
            response = urllib_request.urlopen(outgoing, timeout=proxy.timeout_seconds)
        except urllib_request.HTTPError as exc:  # 4xx/5xx still carry a body
            response = exc
        except Exception as exc:
            payload = json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(response.status if hasattr(response, "status") else response.code)
        for key, value in response.headers.items():
            if key.lower() not in HOP_BY_HOP_HEADERS:
                self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()

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
        self.strip_thinking = strip_thinking
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._seq = 0
        self.tools_logged = False
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

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

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
