"""Layered configuration for the standard benchmark run command."""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import urlsplit

import yaml

from roller.harness_registry import HARNESS_NAMES
from roller.thinking import normalize_thinking


BENCH_RUN_SCHEMA = "roller.bench-run/v1"
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_REFERENCE_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
ANY_ENV_REFERENCE_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


@dataclass(frozen=True)
class BenchRunPlan:
    """Resolved configuration translated to the existing ``collect`` CLI."""

    collect_argv: tuple[str, ...]
    environment: dict[str, str]
    provenance: dict[str, Any]
    resolved: dict[str, Any]


def _load_mapping(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML/JSON in {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain an object: {path}")
    return payload


def _load_extra(value: str, index: int) -> tuple[dict[str, Any], str]:
    candidate = Path(value).expanduser()
    try:
        is_file = candidate.is_file()
    except OSError:
        is_file = False
    if is_file:
        resolved = candidate.resolve()
        return _load_mapping(resolved, "extra config"), str(resolved)
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise FileNotFoundError(
            f"extra config is neither a file nor a JSON object: {value}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError("inline --extra-config must be a JSON object")
    return payload, f"<inline:{index}>"


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _resolve_environment(
    value: Any,
    *,
    environ: Mapping[str, str],
    path: str = "$",
) -> Any:
    if isinstance(value, dict):
        return {
            key: _resolve_environment(item, environ=environ, path=f"{path}.{key}")
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_environment(item, environ=environ, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if not isinstance(value, str):
        return value
    match = ENV_REFERENCE_RE.fullmatch(value)
    if match:
        name = match.group(1)
        resolved = environ.get(name)
        if resolved is None or resolved == "":
            raise ValueError(f"{path} references missing environment variable {name}")
        return resolved
    if ANY_ENV_REFERENCE_RE.search(value):
        raise ValueError(
            f"{path}: environment references must occupy the complete value"
        )
    return value


def _mapping(config: Mapping[str, Any], key: str, *, required: bool = False) -> dict[str, Any]:
    value = config.get(key)
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return dict(value)


def _reject_unknown(section: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(section) - allowed)
    if unknown:
        raise ValueError(f"{label} has unknown field(s): {', '.join(unknown)}")


def _required_string(section: Mapping[str, Any], key: str, label: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}.{key} must be a non-empty string")
    return value.strip()


def _optional_string(section: Mapping[str, Any], key: str, label: str) -> str | None:
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}.{key} must be a non-empty string when set")
    return value.strip()


def _string_list(section: Mapping[str, Any], key: str, label: str) -> list[str]:
    value = section.get(key, [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{label}.{key} must be a list of non-empty strings")
    return [item.strip() for item in value]


def _append_value(argv: list[str], flag: str, value: Any) -> None:
    if value is not None:
        argv.extend((flag, str(value)))


def _append_bool(argv: list[str], flag: str, value: Any, label: str) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be boolean")
    argv.append(flag if value else f"--no-{flag.removeprefix('--')}")


_HARNESS_EXTRA: dict[str, dict[str, tuple[str, str]]] = {
    "openclaw": {
        "model_config": ("--model-config", "value"),
        "gateway_port": ("--gateway-port", "value"),
        "gateway_port_step": ("--gateway-port-step", "value"),
        "gateway_startup_timeout": ("--gateway-startup-timeout", "value"),
        "model_request_timeout": ("--openclaw-model-request-timeout", "value"),
        "max_tokens": ("--openclaw-max-tokens", "value"),
        "session_id": ("--session-id", "value"),
        "search_provider": ("--search-provider", "value"),
        "collect_paths": ("--collect-path", "repeat"),
        "set_image_model": ("--set-image-model", "bool"),
        "cache_trace": ("--cache-trace", "bool"),
    },
    "codex": {
        "config": ("--codex-config", "value"),
        "reasoning_summary": ("--reasoning-summary", "value"),
        "web_search": ("--codex-web-search", "value"),
        "websockets": ("--codex-websockets", "bool"),
    },
    "claude-code": {
        "settings": ("--claude-settings", "value"),
        "max_turns": ("--claude-max-turns", "value"),
        "max_thinking_tokens": ("--claude-max-thinking-tokens", "value"),
        "permission_mode": ("--claude-permission-mode", "value"),
    },
    "hermes": {
        "max_iterations": ("--hermes-max-iterations", "value"),
        "context_length": ("--hermes-context-length", "value"),
        "vision": ("--hermes-vision", "bool"),
    },
    "opencode": {
        "max_steps": ("--opencode-max-steps", "value"),
        "context_length": ("--opencode-context-length", "value"),
        "output_tokens": ("--opencode-output-tokens", "value"),
        "vision": ("--opencode-vision", "bool"),
    },
    "deepseek": {
        "max_steps": ("--deepseek-max-steps", "value"),
        "context_length": ("--deepseek-context-length", "value"),
        "output_tokens": ("--deepseek-output-tokens", "value"),
    },
    "pi": {
        "max_steps": ("--pi-max-steps", "value"),
        "context_length": ("--pi-context-length", "value"),
        "output_tokens": ("--pi-output-tokens", "value"),
    },
}

_HARNESS_API_PROTOCOL = {
    "codex": "openai-responses",
    "claude-code": "anthropic-messages",
    "hermes": "openai-completions",
    "opencode": "openai-completions",
    "deepseek": "openai-completions",
    "pi": "openai-completions",
}

_BENCHMARK_EXTRA: dict[str, dict[str, tuple[str, str]]] = {
    "qwenclawbench": {
        "runs_per_task": ("--bench-runs-per-task", "value"),
    },
}


def _append_benchmark_extra(
    argv: list[str], benchmark: str, extra: Mapping[str, Any]
) -> None:
    supported = _BENCHMARK_EXTRA.get(benchmark, {})
    _reject_unknown(extra, set(supported), "benchmark.extra")
    for name, value in extra.items():
        flag, _kind = supported[name]
        _append_value(argv, flag, value)


def _append_harness_extra(
    argv: list[str], harness: str, extra: Mapping[str, Any]
) -> None:
    supported = _HARNESS_EXTRA[harness]
    _reject_unknown(extra, set(supported), "harness.extra")
    for name, value in extra.items():
        flag, kind = supported[name]
        if kind == "bool":
            _append_bool(argv, flag, value, f"harness.extra.{name}")
        elif kind == "repeat":
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item for item in value
            ):
                raise ValueError(f"harness.extra.{name} must be a string list")
            for item in value:
                argv.extend((flag, item))
        else:
            _append_value(argv, flag, value)


def _openclaw_model_config(
    model: Mapping[str, Any], temporary_root: Path
) -> tuple[str, Path]:
    model_id = _required_string(model, "id", "model")
    provider_override = _optional_string(model, "provider", "model")
    if provider_override is not None:
        provider_id = provider_override
        upstream_model_id = model_id
    elif "/" in model_id:
        # Backward compatibility for the historical provider/model spelling.
        provider_id, upstream_model_id = model_id.split("/", 1)
    else:
        provider_id = "roller"
        upstream_model_id = model_id
    if not provider_id or not upstream_model_id or "/" in provider_id:
        raise ValueError(
            "model.provider must be a non-empty OpenClaw provider name"
        )
    base_url = _required_string(model, "base_url", "model")
    api_key_env = _required_string(model, "api_key_env", "model")
    protocol = str(model.get("api_protocol") or "openai-completions")
    params = model.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("model.params must be an object")
    forbidden = sorted(set(params) & {"id", "name"})
    if forbidden:
        raise ValueError(
            "model.params cannot replace generated OpenClaw field(s): "
            + ", ".join(forbidden)
        )
    payload = {
        "mode": "merge",
        "providers": {
            provider_id: {
                "baseUrl": base_url,
                "apiKey": "${" + api_key_env + "}",
                "api": protocol,
                "models": [
                    {"id": upstream_model_id, "name": upstream_model_id, **params}
                ],
            }
        },
    }
    path = temporary_root / "openclaw-models.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"{provider_id}/{upstream_model_id}", path


def _validate_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("model.base_url must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("model.base_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("model.base_url must not contain a query or fragment")


def _safe_url(value: str | None) -> str | None:
    """Remove URL credentials and query parameters before recording provenance."""

    if value is None:
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return "<invalid-url>"
    if ":" in hostname:
        hostname = f"[{hostname}]"
    netloc = hostname if port is None else f"{hostname}:{port}"
    return parsed._replace(netloc=netloc, query="", fragment="").geturl()


def _redact(value: Any, key: str = "") -> Any:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    sensitive = any(
        marker in normalized
        for marker in ("apikey", "token", "password", "secret", "authorization")
    ) and not normalized.endswith("env")
    if sensitive and isinstance(value, str):
        return "<redacted>"
    if isinstance(value, dict):
        return {name: _redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    return value


def load_bench_run_plan(
    config_path: str | Path,
    extra_configs: Sequence[str] = (),
    *,
    environ: Mapping[str, str] | None = None,
    temporary_root: Path,
) -> BenchRunPlan:
    """Load layered YAML/JSON and translate it to ``roller collect`` arguments."""

    environment = os.environ if environ is None else environ
    base_path = Path(config_path).expanduser().resolve()
    merged = _load_mapping(base_path, "benchmark run config")
    sources = [str(base_path)]
    for index, value in enumerate(extra_configs, start=1):
        overlay, source = _load_extra(value, index)
        merged = _deep_merge(merged, overlay)
        sources.append(source)
    resolved = _resolve_environment(merged, environ=environment)

    _reject_unknown(
        resolved,
        {"schema_version", "benchmark", "model", "harness", "execution"},
        "configuration",
    )
    if resolved.get("schema_version") != BENCH_RUN_SCHEMA:
        raise ValueError(
            f"schema_version must be {BENCH_RUN_SCHEMA!r}"
        )

    benchmark = _mapping(resolved, "benchmark", required=True)
    model = _mapping(resolved, "model", required=True)
    harness = _mapping(resolved, "harness", required=True)
    execution = _mapping(resolved, "execution")
    _reject_unknown(
        benchmark,
        {
            "id",
            "root",
            "variant",
            "categories",
            "task_ids",
            "timeout_multiplier",
            "environment",
            "extra",
        },
        "benchmark",
    )
    _reject_unknown(
        model,
        {
            "id",
            "label",
            "provider",
            "base_url",
            "api_key_env",
            "api_protocol",
            "thinking",
            "params",
        },
        "model",
    )
    _reject_unknown(harness, {"id", "image", "extra"}, "harness")
    _reject_unknown(
        execution,
        {
            "output",
            "run_name",
            "parallel",
            "grader_parallel",
            "samples",
            "retries",
            "timeout",
            "start_index",
            "limit",
            "resume",
            "wire_trace",
            "keep_thinking",
            "pass_env",
            "keep_container_on_error",
            "rebuild_task_images",
            "container_user",
            "network",
            "docker_binary",
            "max_input_file_bytes",
            "max_task_input_bytes",
        },
        "execution",
    )

    benchmark_id = _required_string(benchmark, "id", "benchmark")
    benchmark_root = _required_string(benchmark, "root", "benchmark")
    harness_id = _required_string(harness, "id", "harness")
    if harness_id not in HARNESS_NAMES:
        raise ValueError(
            f"harness.id must be one of: {', '.join(HARNESS_NAMES)}"
        )
    model_id = _required_string(model, "id", "model")
    model_provider = _optional_string(model, "provider", "model")
    base_url = _optional_string(model, "base_url", "model")
    api_key_env = _optional_string(model, "api_key_env", "model")
    api_protocol = _optional_string(model, "api_protocol", "model")
    thinking = normalize_thinking(_optional_string(model, "thinking", "model"))
    bench_environment = benchmark.get("environment", {})
    if not isinstance(bench_environment, dict):
        raise ValueError("benchmark.environment must be an object")
    exported_environment: dict[str, str] = {}
    for name, value in bench_environment.items():
        if not isinstance(name, str) or not ENV_NAME_RE.fullmatch(name):
            raise ValueError(f"benchmark.environment has invalid name: {name!r}")
        if not isinstance(value, str):
            raise ValueError(f"benchmark.environment.{name} must be a string")
        exported_environment[name] = value
    if base_url is not None:
        _validate_url(base_url)
    if api_key_env is not None:
        if not ENV_NAME_RE.fullmatch(api_key_env):
            raise ValueError("model.api_key_env must be an environment variable name")
        if not (exported_environment.get(api_key_env) or environment.get(api_key_env)):
            raise ValueError(
                f"model.api_key_env references missing environment variable {api_key_env}"
            )

    argv = [
        "collect",
        "--bench",
        benchmark_id,
        "--bench-root",
        benchmark_root,
        "--model",
        model_id,
        "--harness",
        harness_id,
    ]
    for category in _string_list(benchmark, "categories", "benchmark"):
        argv.extend(("--bench-category", category))
    for task_id in _string_list(benchmark, "task_ids", "benchmark"):
        argv.extend(("--bench-task-id", task_id))
    _append_value(argv, "--bench-variant", _optional_string(benchmark, "variant", "benchmark"))
    if "timeout_multiplier" in benchmark:
        _append_value(argv, "--bench-timeout-multiplier", benchmark["timeout_multiplier"])
    benchmark_extra = _mapping(benchmark, "extra")
    _append_benchmark_extra(argv, benchmark_id, benchmark_extra)

    model_params = model.get("params", {})
    if not isinstance(model_params, dict):
        raise ValueError("model.params must be an object")
    harness_extra = _mapping(harness, "extra")
    if harness_id == "openclaw":
        custom_model_config = harness_extra.get("model_config")
        if custom_model_config is not None:
            if (
                base_url is not None
                or api_key_env is not None
                or api_protocol is not None
                or model_params
            ):
                raise ValueError(
                    "harness.extra.model_config cannot be combined with model.base_url, "
                    "model.api_key_env, model.api_protocol, or model.params"
                )
        else:
            if base_url is None or api_key_env is None:
                raise ValueError(
                    "OpenClaw requires model.base_url and model.api_key_env when "
                    "harness.extra.model_config is not set"
                )
            model_id, generated = _openclaw_model_config(model, temporary_root)
            argv[argv.index("--model") + 1] = model_id
            harness_extra["model_config"] = str(generated)
    else:
        if base_url is None or api_key_env is None:
            raise ValueError(
                f"{harness_id} requires model.base_url and model.api_key_env"
            )
        expected_protocol = _HARNESS_API_PROTOCOL[harness_id]
        if api_protocol is not None and api_protocol != expected_protocol:
            raise ValueError(
                f"model.api_protocol must be {expected_protocol!r} for {harness_id}"
            )
        if model_params:
            raise ValueError(
                f"model.params is only supported by generated OpenClaw model config; "
                f"use harness.extra for {harness_id}"
            )
        _append_value(argv, "--api-base", base_url)
        _append_value(argv, "--api-key-env", api_key_env)

    _append_value(argv, "--model-label", _optional_string(model, "label", "model"))
    _append_value(argv, "--thinking", thinking)
    _append_value(argv, "--docker-image", _optional_string(harness, "image", "harness"))
    _append_harness_extra(argv, harness_id, harness_extra)

    value_flags = {
        "output": "--output",
        "run_name": "--run-name",
        "parallel": "--parallel",
        "grader_parallel": "--grader-parallel",
        "samples": "--samples",
        "retries": "--retries",
        "timeout": "--timeout",
        "start_index": "--start-index",
        "limit": "--limit",
        "wire_trace": "--wire-trace",
        "container_user": "--container-user",
        "network": "--network",
        "docker_binary": "--docker-binary",
        "max_input_file_bytes": "--max-input-file-bytes",
        "max_task_input_bytes": "--max-task-input-bytes",
    }
    for name, flag in value_flags.items():
        if name in execution:
            _append_value(argv, flag, execution[name])
    bool_flags = {
        "keep_thinking": "--keep-thinking",
        "resume": "--resume",
        "keep_container_on_error": "--keep-container-on-error",
        "rebuild_task_images": "--rebuild-task-images",
    }
    for name, flag in bool_flags.items():
        if name not in execution:
            continue
        value = execution[name]
        if name in {"resume", "keep_container_on_error", "rebuild_task_images"}:
            if not isinstance(value, bool):
                raise ValueError(f"execution.{name} must be boolean")
            if value:
                argv.append(flag)
        else:
            _append_bool(argv, flag, value, f"execution.{name}")
    for name in _string_list(execution, "pass_env", "execution"):
        if not ENV_NAME_RE.fullmatch(name):
            raise ValueError(f"execution.pass_env contains invalid name: {name}")
        argv.extend(("--pass-env", name))

    redacted = _redact(resolved)
    digest = hashlib.sha256(
        json.dumps(redacted, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    provenance = {
        "schema_version": BENCH_RUN_SCHEMA,
        "sources": sources,
        "resolved_sha256": digest,
        "environment_names": sorted(exported_environment),
        "effective": {
            "benchmark": {
                "id": benchmark_id,
                "root": benchmark_root,
                "variant": benchmark.get("variant"),
                "categories": benchmark.get("categories", []),
                "task_ids": benchmark.get("task_ids", []),
                "timeout_multiplier": benchmark.get("timeout_multiplier", 1),
                "extra": _redact(benchmark_extra),
            },
            "model": {
                "id": model_id,
                "label": model.get("label"),
                "provider": model_provider,
                "base_url": _safe_url(base_url),
                "api_key_env": api_key_env,
                "api_protocol": (
                    api_protocol
                    or _HARNESS_API_PROTOCOL.get(harness_id)
                    or ("openai-completions" if base_url is not None else None)
                ),
                "thinking": thinking,
                "params": _redact(model_params),
            },
            "harness": {
                "id": harness_id,
                "image": harness.get("image"),
                "extra": _redact(_mapping(harness, "extra")),
            },
            "execution": _redact(execution),
        },
    }
    return BenchRunPlan(tuple(argv), exported_environment, provenance, resolved)


@contextmanager
def temporary_environment(values: Mapping[str, str]) -> Iterator[None]:
    """Apply benchmark-specific environment values for one in-process run."""

    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


__all__ = [
    "BENCH_RUN_SCHEMA",
    "BenchRunPlan",
    "load_bench_run_plan",
    "temporary_environment",
]
