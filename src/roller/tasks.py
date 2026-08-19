from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from roller.models import ContextFile, InputFile, OutputFile, RunRequest, Task

TASK_SCHEMA_V1 = "roller.task/v1"
CAPTURE_POLICIES = {"full", "hash", "none"}
OUTPUT_TYPES = {"file", "directory", "glob"}
DEFAULT_CONTEXT_TARGETS = {
    "user": "USER.md",
    "memory": "MEMORY.md",
    "system": "SYSTEM.md",
    "soul": "SOUL.md",
    "agents": "AGENTS.md",
}
DEFAULT_CONTEXT_CAPTURE = {"user": "hash", "memory": "hash"}
GLOB_MARKERS = re.compile(r"[*?[]")


class TaskFormatError(ValueError):
    pass


def _string_list(value: Any, field_name: str, index: int) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return tuple(line.strip() for line in value.splitlines() if line.strip())
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(item.strip() for item in value if item.strip())
    raise TaskFormatError(f"task {index}: {field_name!r} must be a string or string list")


def _parse_messages(value: Any, index: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise TaskFormatError(f"task {index}: 'messages' must be a non-empty list")
    prompts: list[str] = []
    for message_index, item in enumerate(value):
        if isinstance(item, str):
            content = item
        elif isinstance(item, dict):
            role = item.get("role", "user")
            content = item.get("content")
            if role != "user":
                raise TaskFormatError(
                    f"task {index}, message {message_index}: only user messages are accepted"
                )
        else:
            raise TaskFormatError(f"task {index}, message {message_index}: invalid message")
        if not isinstance(content, str) or not content.strip():
            raise TaskFormatError(f"task {index}, message {message_index}: empty content")
        prompts.append(content.strip())
    return tuple(prompts)


def _read_rows(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8-sig", errors="strict") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise TaskFormatError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
                if not isinstance(row, dict):
                    raise TaskFormatError(f"{path}:{line_number}: each row must be an object")
                rows.append(row)
        return rows

    if suffix == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise TaskFormatError(f"{path}: invalid JSON: {exc.msg}") from exc
        if isinstance(payload, dict) and isinstance(payload.get("tasks"), list):
            payload = payload["tasks"]
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise TaskFormatError(f"{path}: expected a list of task objects or {{'tasks': [...]}}")
        return payload

    raise TaskFormatError(f"unsupported task-list extension {suffix!r}; use .jsonl or .json")


def _resolve_source(value: Any, base_dir: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise TaskFormatError(f"{label}: source must be a non-empty path string")
    source = Path(value).expanduser()
    if not source.is_absolute():
        source = base_dir / source
    return Path(os.path.abspath(source))


def _resolve_file_root(row: dict[str, Any], task_list: Path, index: int) -> Path:
    value = row.get("file_root")
    if value is None:
        return task_list.parent
    if not isinstance(value, str) or not value.strip():
        raise TaskFormatError(f"task {index}: file_root must be a non-empty path string")
    root = Path(value).expanduser()
    if not root.is_absolute():
        root = task_list.parent / root
    root = Path(os.path.abspath(root))
    if not root.is_dir():
        raise TaskFormatError(f"task {index}: file_root is not a directory: {root}")
    return root


def normalize_relative_path(
    value: Any,
    label: str,
    *,
    allow_dot: bool = False,
    allow_glob: bool = False,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskFormatError(f"{label}: path must be a non-empty string")
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/"):
        raise TaskFormatError(f"{label}: absolute paths are not allowed: {raw!r}")
    path = PurePosixPath(raw)
    if any(part == ".." for part in path.parts):
        raise TaskFormatError(f"{label}: parent traversal is not allowed: {raw!r}")
    if not allow_glob and GLOB_MARKERS.search(raw):
        raise TaskFormatError(f"{label}: glob characters are not allowed: {raw!r}")
    normalized = path.as_posix().rstrip("/") or "."
    if normalized == "." and not allow_dot:
        raise TaskFormatError(f"{label}: '.' is not allowed here")
    return normalized


def _parse_inputs(value: Any, base_dir: Path, index: int) -> tuple[InputFile, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TaskFormatError(f"task {index}: files.inputs must be a list")
    inputs: list[InputFile] = []
    for file_index, item in enumerate(value):
        label = f"task {index}, input {file_index}"
        if isinstance(item, str):
            source = _resolve_source(item, base_dir, label)
            target_value = source.name
            required = True
            expected_sha256 = None
        elif isinstance(item, dict):
            source = _resolve_source(item.get("source"), base_dir, label)
            target_value = item.get("target", source.name)
            required = item.get("required", True)
            expected_sha256 = item.get("sha256")
        else:
            raise TaskFormatError(f"{label}: expected a source string or object")
        if not isinstance(required, bool):
            raise TaskFormatError(f"{label}: required must be boolean")
        if expected_sha256 is not None:
            if not isinstance(expected_sha256, str) or not re.fullmatch(
                r"[0-9a-fA-F]{64}", expected_sha256
            ):
                raise TaskFormatError(f"{label}: sha256 must contain exactly 64 hex characters")
            expected_sha256 = expected_sha256.lower()
        inputs.append(
            InputFile(
                source=source,
                target=normalize_relative_path(
                    target_value, f"{label}.target", allow_dot=source.is_dir()
                ),
                required=required,
                expected_sha256=expected_sha256,
            )
        )
    return tuple(inputs)


def _default_context_target(role: str, source: Path) -> str:
    if role in DEFAULT_CONTEXT_TARGETS:
        return DEFAULT_CONTEXT_TARGETS[role]
    if role in {"skill", "skills"}:
        return f"skills/{source.name}"
    return source.name


def _parse_context(value: Any, base_dir: Path, index: int) -> tuple[ContextFile, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TaskFormatError(f"task {index}: files.context must be a list")
    context_files: list[ContextFile] = []
    for file_index, item in enumerate(value):
        label = f"task {index}, context {file_index}"
        if not isinstance(item, dict):
            raise TaskFormatError(f"{label}: context entries must be objects")
        role = item.get("role")
        if not isinstance(role, str) or not role.strip():
            raise TaskFormatError(f"{label}: role must be a non-empty string")
        role = role.strip().lower()
        source = _resolve_source(item.get("source"), base_dir, label)
        target = normalize_relative_path(
            item.get("target", _default_context_target(role, source)),
            f"{label}.target",
            allow_dot=source.is_dir(),
        )
        if PurePosixPath(target).parts and PurePosixPath(target).parts[0] in {
            "inputs",
            "outputs",
            "context",
        }:
            raise TaskFormatError(
                f"{label}.target: top-level inputs, outputs and context are reserved"
            )
        capture = item.get("capture", DEFAULT_CONTEXT_CAPTURE.get(role, "full"))
        if capture not in CAPTURE_POLICIES:
            raise TaskFormatError(
                f"{label}: capture must be one of {sorted(CAPTURE_POLICIES)}"
            )
        required = item.get("required", True)
        if not isinstance(required, bool):
            raise TaskFormatError(f"{label}: required must be boolean")
        if role in DEFAULT_CONTEXT_TARGETS and source.exists() and source.is_dir():
            raise TaskFormatError(f"{label}: role {role!r} requires a file source")
        context_files.append(ContextFile(role, source, target, capture, required))
    return tuple(context_files)


def _parse_outputs(value: Any, index: int) -> tuple[OutputFile, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TaskFormatError(f"task {index}: files.outputs must be a list")
    outputs: list[OutputFile] = []
    for file_index, item in enumerate(value):
        label = f"task {index}, output {file_index}"
        if isinstance(item, str):
            path_value = item
            output_type = "glob" if GLOB_MARKERS.search(item) else "file"
            required = True
            max_bytes = None
        elif isinstance(item, dict):
            path_value = item.get("path")
            output_type = item.get("type")
            if output_type is None:
                output_type = (
                    "glob"
                    if isinstance(path_value, str) and GLOB_MARKERS.search(path_value)
                    else "file"
                )
            required = item.get("required", True)
            max_bytes = item.get("max_bytes")
        else:
            raise TaskFormatError(f"{label}: expected a path string or object")
        if output_type not in OUTPUT_TYPES:
            raise TaskFormatError(f"{label}: type must be one of {sorted(OUTPUT_TYPES)}")
        if not isinstance(required, bool):
            raise TaskFormatError(f"{label}: required must be boolean")
        if max_bytes is not None and (
            not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0
        ):
            raise TaskFormatError(f"{label}: max_bytes must be a positive integer")
        outputs.append(
            OutputFile(
                path=normalize_relative_path(
                    path_value,
                    f"{label}.path",
                    allow_dot=output_type == "directory",
                    allow_glob=output_type == "glob",
                ),
                type=output_type,
                required=required,
                max_bytes=max_bytes,
            )
        )
    return tuple(outputs)


def _parse_output_dir(value: Any, index: int) -> str:
    output_dir = normalize_relative_path(
        "outputs" if value is None else value,
        f"task {index}, files.output_dir",
    )
    first_part = PurePosixPath(output_dir).parts[0]
    if first_part in {"inputs", "context"}:
        raise TaskFormatError(
            f"task {index}, files.output_dir: top-level inputs and context are reserved"
        )
    return output_dir


def _validate_output_context_paths(
    output_dir: str, context_files: tuple[ContextFile, ...], index: int
) -> None:
    output_prefix = f"{output_dir}/"
    for context in context_files:
        context_prefix = f"{context.target}/"
        if (
            context.target == output_dir
            or context.target.startswith(output_prefix)
            or output_dir.startswith(context_prefix)
        ):
            raise TaskFormatError(
                f"task {index}: files.output_dir {output_dir!r} overlaps context target "
                f"{context.target!r}"
            )


def _legacy_persona_context(
    row: dict[str, Any], workspace: Path | None, index: int
) -> tuple[ContextFile, ...]:
    persona = row.get("persona")
    if not isinstance(persona, str) or not persona.strip() or workspace is None:
        return ()
    persona_root = (workspace / persona.strip()).resolve()
    try:
        persona_root.relative_to(workspace.resolve())
    except ValueError as exc:
        raise TaskFormatError(f"task {index}: persona escapes workspace") from exc
    return (
        ContextFile("user", persona_root / "USER.md", "USER.md", "hash", False),
        ContextFile("memory", persona_root / "MEMORY.md", "MEMORY.md", "hash", False),
    )


def load_tasks(
    path: Path,
    *,
    prompt_key: str = "prompt",
    id_key: str = "id",
    global_workspace: Path | None = None,
) -> list[Task]:
    """Load v1 tasks and normalize legacy Interns2_data rows into the same model."""

    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"task list not found: {path}")
    rows = _read_rows(path)
    tasks: list[Task] = []
    seen_ids: set[str] = set()

    for index, row in enumerate(rows):
        schema_version = row.get("schema_version", TASK_SCHEMA_V1)
        if schema_version != TASK_SCHEMA_V1:
            raise TaskFormatError(
                f"task {index}: unsupported schema_version {schema_version!r}; expected {TASK_SCHEMA_V1!r}"
            )
        task_id_value = row.get(id_key, f"task_{index:08d}")
        if not isinstance(task_id_value, (str, int)) or not str(task_id_value).strip():
            raise TaskFormatError(f"task {index}: {id_key!r} must be a non-empty string or integer")
        task_id = str(task_id_value).strip()
        if task_id in seen_ids:
            raise TaskFormatError(f"task {index}: duplicate task id {task_id!r}")
        seen_ids.add(task_id)

        if "messages" in row:
            prompts = _parse_messages(row["messages"], index)
        else:
            prompt = row.get(prompt_key)
            if prompt is None and prompt_key == "prompt":
                prompt = row.get("question")
            if not isinstance(prompt, str) or not prompt.strip():
                raise TaskFormatError(
                    f"task {index}: missing non-empty {prompt_key!r} (or compatibility key 'question')"
                )
            prompts = (prompt.strip(),)

        workspace = global_workspace
        row_workspace = row.get("workspace") or row.get("workspace_path")
        if row_workspace is not None:
            if not isinstance(row_workspace, str) or not row_workspace.strip():
                raise TaskFormatError(f"task {index}: workspace must be a non-empty path string")
            workspace = Path(row_workspace).expanduser()
            if not workspace.is_absolute():
                workspace = path.parent / workspace
            workspace = workspace.resolve()
        elif workspace is not None:
            workspace = workspace.expanduser().resolve()

        file_root = _resolve_file_root(row, path, index)
        files = row.get("files", {})
        if not isinstance(files, dict):
            raise TaskFormatError(f"task {index}: files must be an object")
        output_dir = _parse_output_dir(files.get("output_dir"), index)
        inputs = _parse_inputs(files.get("inputs"), file_root, index)
        legacy_workspace = workspace is not None and not inputs
        if legacy_workspace:
            inputs = (InputFile(workspace, ".", True, None),)
        context_files = _parse_context(files.get("context"), file_root, index)
        context_files += _legacy_persona_context(row, workspace, index)
        _validate_output_context_paths(output_dir, context_files, index)
        outputs = _parse_outputs(files.get("outputs"), index)

        model = row.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise TaskFormatError(f"task {index}: model must be a non-empty string")
        runtime = row.get("runtime", {})
        if not isinstance(runtime, dict):
            raise TaskFormatError(f"task {index}: runtime must be an object")
        thinking = runtime.get("thinking", row.get("thinking"))
        if thinking is not None and not isinstance(thinking, str):
            raise TaskFormatError(f"task {index}: thinking must be a string")

        tasks.append(
            Task(
                id=task_id,
                prompts=prompts,
                source_index=index,
                raw=row,
                schema_version=schema_version,
                inputs=inputs,
                context_files=context_files,
                outputs=outputs,
                output_dir=output_dir,
                legacy_workspace=legacy_workspace,
                workspace=workspace,
                model=model.strip() if isinstance(model, str) else None,
                env_names=_string_list(runtime.get("env", row.get("env")), "runtime.env", index),
                setup_commands=_string_list(
                    runtime.get("setup", row.get("setup", row.get("warmup"))),
                    "runtime.setup",
                    index,
                ),
                thinking=thinking,
            )
        )
    return tasks


def build_requests(
    tasks: Iterable[Task], models: list[str] | None, timeout_seconds: int
) -> list[RunRequest]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    normalized_models = list(
        dict.fromkeys(model.strip() for model in (models or []) if model.strip())
    )
    requests: list[RunRequest] = []
    for task in tasks:
        task_models = normalized_models or ([task.model] if task.model else [])
        if not task_models:
            raise TaskFormatError(
                f"task {task.id!r} has no model; pass --model or set the task's 'model' field"
            )
        for model in task_models:
            requests.append(
                RunRequest(len(requests), task, model, timeout_seconds)
            )
    return requests
