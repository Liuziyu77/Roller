from __future__ import annotations

import base64
import json
import logging
import mimetypes
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 80
MAX_PAGE_SIZE = 200
DEFAULT_TEXT_LIMIT = 40_000
MAX_TEXT_LIMIT = 1_000_000


class ViewerDataError(ValueError):
    """Raised when a run contains data the viewer cannot safely interpret."""


class ViewerNotFound(ViewerDataError):
    """Raised when a requested rollout or trajectory does not exist."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ViewerNotFound(f"file not found: {path.name}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ViewerDataError(f"cannot read JSON file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ViewerDataError(f"JSON file must contain an object: {path}")
    return payload


def _inside(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ViewerDataError(f"path escapes the selected run: {path}") from exc
    return resolved


def _rollout_id(relative_path: str) -> str:
    encoded = base64.urlsafe_b64encode(relative_path.encode("utf-8")).decode("ascii")
    return encoded.rstrip("=")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _integer(value: Any, default: int = 0) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _number(value: Any) -> int | float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            else:
                parts.append(json.dumps(block, ensure_ascii=False, indent=2))
        return "\n".join(parts)
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


def _text_view(value: Any, limit: int) -> dict[str, Any]:
    content = _content_text(value)
    return {
        "text": content[:limit],
        "length": len(content),
        "truncated": len(content) > limit,
    }


def _preview(value: Any, limit: int = 180) -> str:
    return " ".join(_content_text(value).split())[:limit]


def _duration_seconds(started_at: Any, finished_at: Any) -> float | None:
    if not isinstance(started_at, str) or not isinstance(finished_at, str):
        return None

    def parse(value: str) -> datetime:
        normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(normalized)

    try:
        return round((parse(finished_at) - parse(started_at)).total_seconds(), 3)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Rollout:
    rollout_id: str
    directory: Path
    relative_path: str
    info: dict[str, Any]

    def trace_path(self, root: Path) -> Path | None:
        trajectory = _mapping(self.info.get("trajectory"))
        value = trajectory.get("actf")
        name = value if isinstance(value, str) and value else "trace_ACTF.json"
        try:
            path = _inside(root, self.directory / name)
        except ViewerDataError:
            return None
        return path if path.is_file() else None

    def summary(self, root: Path) -> dict[str, Any]:
        task_id = _string(self.info.get("task_id")) or self.directory.name
        model = _string(self.info.get("model")) or self.directory.parent.name
        usage = _mapping(self.info.get("usage"))
        timing = _mapping(self.info.get("timing"))
        conversation = _mapping(self.info.get("conversation"))
        output_collection = _mapping(self.info.get("output_collection"))
        return {
            "id": self.rollout_id,
            "task_id": task_id,
            "model": model,
            "harness": _string(self.info.get("harness")),
            "status": _string(self.info.get("status")) or "partial",
            "error": _string(self.info.get("error")),
            "request_index": self.info.get("request_index"),
            "source_index": self.info.get("source_index"),
            "path": self.relative_path,
            "usage": usage,
            "timing": timing,
            "attempt_count": len(self.info.get("attempts", []))
            if isinstance(self.info.get("attempts"), list)
            else 0,
            "has_actf": self.trace_path(root) is not None,
            "has_conversation": bool(conversation.get("path"))
            or (self.directory / "conversation.json").is_file(),
            "conversation": conversation,
            "output_file_count": len(output_collection.get("files", []))
            if isinstance(output_collection.get("files"), list)
            else 0,
        }


class RollerRun:
    """Read-only projection of one Roller run for the browser UI."""

    def __init__(self, run_dir: Path | str, cache_size: int = 8) -> None:
        root = Path(run_dir).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"run directory not found: {root}")
        if not (root / "info.json").is_file() and not (root / "samples").is_dir():
            raise ViewerDataError(
                f"not a Roller run (missing info.json and samples/): {root}"
            )
        self.root = root
        self.cache_size = max(1, cache_size)
        self._lock = threading.RLock()
        self._run_info: dict[str, Any] = {}
        self._rollouts: dict[str, Rollout] = {}
        self._actf_cache: OrderedDict[
            str, tuple[tuple[int, int], dict[str, Any]]
        ] = OrderedDict()
        self.refresh()

    def refresh(self) -> None:
        with self._lock:
            info_path = self.root / "info.json"
            self._run_info = _read_json(info_path) if info_path.is_file() else {}
            candidates: dict[str, dict[str, Any]] = {}

            entries = self._run_info.get("samples", [])
            if isinstance(entries, list):
                for entry in entries:
                    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                        continue
                    try:
                        directory = _inside(self.root, self.root / entry["path"])
                    except ViewerDataError:
                        logger.warning("ignoring sample path outside run: %s", entry["path"])
                        continue
                    if directory.is_dir():
                        candidates[directory.relative_to(self.root).as_posix()] = dict(entry)

            samples_root = self.root / "samples"
            if samples_root.is_dir():
                for info_path in sorted(samples_root.glob("*/*/info.json")):
                    try:
                        safe_info = _inside(self.root, info_path)
                        sample_info = _read_json(safe_info)
                    except ViewerDataError as exc:
                        logger.warning("ignoring unreadable sample info %s: %s", info_path, exc)
                        continue
                    relative = safe_info.parent.relative_to(self.root).as_posix()
                    # Sample info carries the finalized trajectory details, while the
                    # run checkpoint carries this invocation's status (for example,
                    # ``skipped`` after Resume). Preserve that run-level overlay.
                    candidates[relative] = {**sample_info, **candidates.get(relative, {})}

            rollouts: dict[str, Rollout] = {}
            for relative, info in sorted(candidates.items()):
                directory = _inside(self.root, self.root / relative)
                rollout_id = _rollout_id(relative)
                rollouts[rollout_id] = Rollout(
                    rollout_id=rollout_id,
                    directory=directory,
                    relative_path=relative,
                    info=info,
                )
            self._rollouts = rollouts

    def run_summary(self) -> dict[str, Any]:
        with self._lock:
            summaries = [item.summary(self.root) for item in self._rollouts.values()]
            status_counts: dict[str, int] = {}
            models: set[str] = set()
            tasks: set[str] = set()
            for item in summaries:
                status = item["status"]
                status_counts[status] = status_counts.get(status, 0) + 1
                if item["model"]:
                    models.add(item["model"])
                tasks.add(item["task_id"])
            configuration = _mapping(self._run_info.get("configuration"))
            harness_name = _string(configuration.get("harness"))
            harness_configuration = _mapping(configuration.get(harness_name))
            if harness_name == "mixed":
                # Mixed task-agent runs retain one config block per Harness;
                # use the first agent block for legacy scalar fields while
                # exposing the complete participant list below.
                agent_names = configuration.get("agent_harnesses")
                if isinstance(agent_names, list):
                    for name in agent_names:
                        candidate = _mapping(configuration.get(name))
                        if candidate:
                            harness_configuration = candidate
                            break
            progress = _mapping(self._run_info.get("summary"))
            return {
                "schema_version": "roller.viewer.run/v1",
                "name": self.root.name,
                "path": str(self.root),
                "roller_schema": self._run_info.get("schema_version"),
                "roller_version": self._run_info.get("roller_version"),
                "started_at": self._run_info.get("started_at"),
                "finished_at": self._run_info.get("finished_at"),
                "updated_at": self._run_info.get("updated_at"),
                "state": self._run_info.get("state"),
                "progress": {
                    "total": progress.get("total"),
                    "processed": progress.get("processed"),
                    "pending": progress.get("pending"),
                },
                "task_count": len(tasks),
                "rollout_count": len(summaries),
                "status_counts": status_counts,
                "models": sorted(models),
                "usage": _mapping(_mapping(self._run_info.get("summary")).get("usage")),
                "configuration": {
                    "harness": harness_name,
                    "agent_harnesses": configuration.get("agent_harnesses", []),
                    "simulator_harnesses": configuration.get("simulator_harnesses", []),
                    "trajectory_count": configuration.get("trajectory_count"),
                    "thinking": harness_configuration.get(
                        "thinking", harness_configuration.get("reasoning_effort")
                    ),
                    "external_thinking": harness_configuration.get(
                        "external_thinking", "off"
                    ),
                    "keep_thinking": harness_configuration.get("keep_thinking"),
                    "reasoning_summary": harness_configuration.get(
                        "reasoning_summary"
                    ),
                    "supports_websockets": harness_configuration.get(
                        "supports_websockets"
                    ),
                },
            }

    def list_tasks(
        self,
        *,
        query: str = "",
        status: str = "",
        model: str = "",
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        page = max(1, page)
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        needle = query.strip().casefold()
        with self._lock:
            selected: list[dict[str, Any]] = []
            for rollout in self._rollouts.values():
                item = rollout.summary(self.root)
                if status and item["status"] != status:
                    continue
                if model and item["model"] != model:
                    continue
                haystack = "\n".join(
                    (item["task_id"], item["model"], item["status"], item["error"])
                ).casefold()
                if needle and needle not in haystack:
                    continue
                selected.append(item)

        selected.sort(
            key=lambda item: (
                _integer(item.get("source_index"), 2**31 - 1),
                item["task_id"],
                item["model"],
            )
        )
        grouped: dict[str, dict[str, Any]] = {}
        for item in selected:
            group = grouped.setdefault(
                item["task_id"],
                {
                    "task_id": item["task_id"],
                    "source_index": item.get("source_index"),
                    "rollouts": [],
                },
            )
            group["rollouts"].append(item)
        tasks = list(grouped.values())
        start = (page - 1) * page_size
        return {
            "page": page,
            "page_size": page_size,
            "total": len(tasks),
            "items": tasks[start : start + page_size],
        }

    def _rollout(self, rollout_id: str) -> Rollout:
        with self._lock:
            rollout = self._rollouts.get(rollout_id)
        if rollout is None:
            raise ViewerNotFound(f"unknown rollout: {rollout_id}")
        return rollout

    def _load_actf(self, rollout: Rollout) -> dict[str, Any] | None:
        path = rollout.trace_path(self.root)
        if path is None:
            return None
        stat = path.stat()
        version = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._actf_cache.get(rollout.rollout_id)
            if cached is not None and cached[0] == version:
                self._actf_cache.move_to_end(rollout.rollout_id)
                return cached[1]
        payload = _read_json(path)
        steps = payload.get("steps")
        if not isinstance(steps, list):
            raise ViewerDataError(f"ACTF steps must be a list: {path}")
        with self._lock:
            self._actf_cache[rollout.rollout_id] = (version, payload)
            self._actf_cache.move_to_end(rollout.rollout_id)
            while len(self._actf_cache) > self.cache_size:
                self._actf_cache.popitem(last=False)
        return payload

    @staticmethod
    def _step_summary(index: int, step: Any) -> dict[str, Any]:
        if not isinstance(step, dict):
            return {"index": index, "step_id": index + 1, "invalid": True}
        assistant = _mapping(step.get("assistant_content"))
        calls = assistant.get("tool_calls", [])
        observations = step.get("observation", [])
        call_rows = calls if isinstance(calls, list) else []
        observation_rows = observations if isinstance(observations, list) else []
        return {
            "index": index,
            "step_id": step.get("step_id", index + 1),
            "started_at": step.get("started_at"),
            "finished_at": step.get("finished_at"),
            "duration_seconds": _duration_seconds(
                step.get("started_at"), step.get("finished_at")
            ),
            "metric": _mapping(step.get("metric")),
            "user_preview": _preview(step.get("user_content")),
            "reasoning_preview": _preview(assistant.get("reasoning_content")),
            "content_preview": _preview(assistant.get("content")),
            "reasoning_chars": len(_content_text(assistant.get("reasoning_content"))),
            "content_chars": len(_content_text(assistant.get("content"))),
            "tool_call_count": len(call_rows),
            "observation_count": len(observation_rows),
            "has_tool_error": any(
                isinstance(row, dict) and bool(row.get("isError", False))
                for row in observation_rows
            ),
        }

    def rollout_detail(self, rollout_id: str) -> dict[str, Any]:
        rollout = self._rollout(rollout_id)
        actf = self._load_actf(rollout)
        conversation_path = rollout.directory / "conversation.json"
        conversation: dict[str, Any] | None = None
        conversation_error: str | None = None
        if conversation_path.is_file():
            try:
                conversation = _read_json(_inside(self.root, conversation_path))
            except ViewerDataError as exc:
                conversation_error = str(exc)
        payload: dict[str, Any] = {
            "rollout": rollout.summary(self.root),
            "info": rollout.info,
            "conversation": conversation,
            "conversation_error": conversation_error,
            "trajectory": None,
        }
        if actf is None:
            return payload
        steps = actf["steps"]
        tools = actf.get("tools")
        payload["trajectory"] = {
            "schema_version": actf.get("schema_version"),
            "started_at": actf.get("started_at"),
            "finished_at": actf.get("finished_at"),
            "system_prompt": _text_view(actf.get("system_prompt"), MAX_TEXT_LIMIT),
            "tools": tools if isinstance(tools, list) else [],
            "tools_captured": isinstance(tools, list),
            "step_count": len(steps),
            "steps": [self._step_summary(index, step) for index, step in enumerate(steps)],
        }
        return payload

    def step_detail(
        self, rollout_id: str, step_index: int, text_limit: int = DEFAULT_TEXT_LIMIT
    ) -> dict[str, Any]:
        rollout = self._rollout(rollout_id)
        actf = self._load_actf(rollout)
        if actf is None:
            raise ViewerNotFound("this rollout has no ACTF trajectory")
        steps = actf["steps"]
        if step_index < 0 or step_index >= len(steps):
            raise ViewerNotFound(f"step index out of range: {step_index}")
        step = steps[step_index]
        if not isinstance(step, dict):
            raise ViewerDataError(f"ACTF step {step_index} must be an object")
        text_limit = max(1_000, min(text_limit, MAX_TEXT_LIMIT))
        assistant = _mapping(step.get("assistant_content"))
        raw_calls = assistant.get("tool_calls", [])
        raw_observations = step.get("observation", [])
        calls: list[dict[str, Any]] = []
        if isinstance(raw_calls, list):
            for call in raw_calls:
                if not isinstance(call, dict):
                    continue
                function = _mapping(call.get("function"))
                name = _string(call.get("name")) or _string(function.get("name"))
                arguments = call.get("arguments", function.get("arguments", {}))
                calls.append(
                    {
                        "id": _string(call.get("id")),
                        "name": name,
                        "type": call.get("type"),
                        "arguments": _text_view(arguments, text_limit),
                    }
                )
        observations: list[dict[str, Any]] = []
        if isinstance(raw_observations, list):
            for observation in raw_observations:
                if not isinstance(observation, dict):
                    continue
                observations.append(
                    {
                        "tool_call_id": _string(
                            observation.get("toolCallId", observation.get("tool_call_id"))
                        ),
                        "tool_name": _string(observation.get("toolName")),
                        "is_error": bool(observation.get("isError", False)),
                        "timestamp": observation.get("timestamp"),
                        "content": _text_view(observation.get("content"), text_limit),
                    }
                )
        return {
            "index": step_index,
            "step_id": step.get("step_id", step_index + 1),
            "started_at": step.get("started_at"),
            "finished_at": step.get("finished_at"),
            "duration_seconds": _duration_seconds(
                step.get("started_at"), step.get("finished_at")
            ),
            "metric": _mapping(step.get("metric")),
            "user": _text_view(step.get("user_content"), text_limit),
            "assistant": {
                "reasoning": _text_view(assistant.get("reasoning_content"), text_limit),
                "content": _text_view(assistant.get("content"), text_limit),
                "tool_calls": calls,
            },
            "observations": observations,
            "text_limit": text_limit,
        }


class ViewerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _first(query: dict[str, list[str]], name: str, default: str = "") -> str:
    values = query.get(name)
    return values[0] if values else default


def _query_int(
    query: dict[str, list[str]], name: str, default: int, minimum: int = 0
) -> int:
    raw = _first(query, name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ViewerDataError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ViewerDataError(f"{name} must be at least {minimum}")
    return value


def _handler(source: RollerRun) -> type[BaseHTTPRequestHandler]:
    static_root = Path(__file__).with_name("static")

    class Handler(BaseHTTPRequestHandler):
        server_version = "RollerViewer/1.0"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            try:
                if parsed.path.startswith("/api/"):
                    self._api(parsed.path, parse_qs(parsed.query))
                else:
                    self._static(parsed.path)
            except ViewerNotFound as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ViewerDataError as exc:
                self._json({"error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            except Exception as exc:  # pragma: no cover - defensive server boundary
                logger.exception("viewer request failed")
                self._json(
                    {"error": f"internal viewer error: {type(exc).__name__}"},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )

        def _api(self, path: str, query: dict[str, list[str]]) -> None:
            if path == "/api/v1/run":
                if _first(query, "refresh") == "1":
                    source.refresh()
                self._json(source.run_summary())
                return
            if path == "/api/v1/tasks":
                self._json(
                    source.list_tasks(
                        query=_first(query, "query"),
                        status=_first(query, "status"),
                        model=_first(query, "model"),
                        page=_query_int(query, "page", 1, 1),
                        page_size=_query_int(
                            query, "page_size", DEFAULT_PAGE_SIZE, 1
                        ),
                    )
                )
                return
            parts = [part for part in path.split("/") if part]
            if len(parts) == 4 and parts[:3] == ["api", "v1", "rollouts"]:
                self._json(source.rollout_detail(parts[3]))
                return
            if (
                len(parts) == 6
                and parts[:3] == ["api", "v1", "rollouts"]
                and parts[4] == "steps"
            ):
                try:
                    step_index = int(parts[5])
                except ValueError as exc:
                    raise ViewerDataError("step index must be an integer") from exc
                self._json(
                    source.step_detail(
                        parts[3],
                        step_index,
                        _query_int(
                            query, "limit", DEFAULT_TEXT_LIMIT, minimum=1_000
                        ),
                    )
                )
                return
            raise ViewerNotFound(f"unknown API endpoint: {path}")

        def _static(self, path: str) -> None:
            names = {
                "/": "index.html",
                "/index.html": "index.html",
                "/app.js": "app.js",
                "/style.css": "style.css",
            }
            name = names.get(path)
            if name is None:
                raise ViewerNotFound(f"page not found: {path}")
            file_path = static_root / name
            try:
                body = file_path.read_bytes()
            except OSError as exc:
                raise ViewerNotFound(f"viewer asset is missing: {name}") from exc
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self._headers(content_type, len(body), cache="public, max-age=300")
            self.end_headers()
            self.wfile.write(body)

        def _json(
            self, payload: Any, status: HTTPStatus = HTTPStatus.OK
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            self.send_response(status)
            self._headers("application/json; charset=utf-8", len(body), cache="no-store")
            self.end_headers()
            self.wfile.write(body)

        def _headers(self, content_type: str, length: int, *, cache: str) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'none'",
            )

        def log_message(self, message: str, *args: Any) -> None:
            logger.info("viewer %s - %s", self.address_string(), message % args)

    return Handler


def make_server(
    run_dir: Path | str, host: str = "127.0.0.1", port: int = 8765
) -> ViewerHTTPServer:
    if not host:
        raise ValueError("viewer host cannot be empty")
    if port < 0 or port > 65_535:
        raise ValueError("viewer port must be between 0 and 65535")
    source = RollerRun(run_dir)
    try:
        return ViewerHTTPServer((host, port), _handler(source))
    except OSError as exc:
        raise RuntimeError(f"cannot start viewer on {host}:{port}: {exc}") from exc


def serve(run_dir: Path | str, host: str = "127.0.0.1", port: int = 8765) -> int:
    server = make_server(run_dir, host, port)
    bound_host, bound_port = server.server_address[:2]
    display_host = "127.0.0.1" if bound_host in {"0.0.0.0", "::"} else bound_host
    print(f"Roller Viewer: http://{display_host}:{bound_port}")
    print(f"Run directory: {Path(run_dir).expanduser().resolve()}")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            "Warning: the viewer is reachable beyond loopback and may expose prompts, "
            "reasoning, tool results, and run metadata."
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nRoller Viewer stopped.")
    finally:
        server.server_close()
    return 0
