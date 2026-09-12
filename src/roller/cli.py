from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from roller.collector import CollectionConfig, Collector
from roller.benches import (
    binding_from_task_row,
    format_report,
    get_bench,
    list_benches,
    mean_aggregate,
)
from roller.benches.grading import GradedHarness
from roller.bundles import BundlePolicy, TaskBundleStore
from roller.conversation import ConversationManager
from roller.external_thinking import validate_external_thinking_provider
from roller.harnesses.claude_code import (
    COMMON_ENV_NAMES as CLAUDE_CODE_COMMON_ENV_NAMES,
    DEFAULT_CLAUDE_CODE_IMAGE,
    CLAUDE_PERMISSION_MODES,
    ClaudeCodeConfig,
    ClaudeCodeHarness,
    claude_effort,
    redact_anthropic_base_url,
)
from roller.harness_registry import HARNESSES, HARNESS_NAMES
from roller.harnesses.deepseek import (
    COMMON_ENV_NAMES as DEEPSEEK_COMMON_ENV_NAMES,
    DEFAULT_DEEPSEEK_IMAGE, DEEPSEEK_VERSION, DEEPSEEK_TOOLS,
    DeepSeekConfig, DeepSeekHarness, deepseek_effort,
)
from roller.harnesses.opencode import (
    COMMON_ENV_NAMES as OPENCODE_COMMON_ENV_NAMES,
    DEFAULT_OPENCODE_IMAGE, OPENCODE_VERSION, OPENCODE_TOOLS,
    OpenCodeConfig, OpenCodeHarness, opencode_effort,
)
from roller.harnesses.hermes import (
    COMMON_ENV_NAMES as HERMES_COMMON_ENV_NAMES,
    DEFAULT_HERMES_IMAGE, HERMES_REVISION, HermesConfig, HermesHarness, hermes_effort,
)
from roller.harnesses.codex import (
    COMMON_ENV_NAMES as CODEX_COMMON_ENV_NAMES,
    DEFAULT_CODEX_IMAGE,
    CodexConfig,
    CodexHarness,
    redact_api_base,
)
from roller.harnesses.openclaw import (
    COMMON_ENV_NAMES as OPENCLAW_COMMON_ENV_NAMES,
    DEFAULT_OPENCLAW_IMAGE,
    OpenClawConfig,
    OpenClawHarness,
    load_model_config,
)
from roller.run_lock import (
    RUN_LOCK_SCHEMA,
    build_run_lock,
    complete_legacy_adoption,
    ensure_run_lock,
)
from roller.storage import atomic_write_json, safe_component
from roller.tasks import TaskFormatError, build_requests, load_tasks
from roller.thinking import THINKING_LEVELS, normalize_thinking, resolve_thinking
from roller.version import __version__
from roller.viewer import serve as serve_viewer

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="roller",
        description="Collect agentic trajectories from a task list.",
    )
    parser.add_argument("--version", action="version", version=f"Roller {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    view = subparsers.add_parser("view", help="browse a Roller run in a local web UI")
    view.add_argument("run_dir", help="Roller run directory containing info.json/samples")
    view.add_argument("--host", default="127.0.0.1", help="listen host (default: loopback)")
    view.add_argument("--port", type=int, default=8765, help="listen port (default: 8765)")

    bench = subparsers.add_parser("bench", help="work with benchmark adapters")
    bench_sub = bench.add_subparsers(dest="bench_command", required=True)

    bench_sub.add_parser("list", help="list registered benchmark adapters")

    bench_export = bench_sub.add_parser(
        "export", help="convert a checked-out bench into a Roller task list"
    )
    bench_export.add_argument("name", help="bench adapter name (see 'roller bench list')")
    bench_export.add_argument("root", help="path to the checked-out benchmark")
    bench_export.add_argument("--output", "-o", required=True, help="task JSONL to write")
    bench_export.add_argument(
        "--category",
        action="append",
        default=[],
        help="restrict to a bench category (repeatable)",
    )
    bench_export.add_argument(
        "--task-id",
        action="append",
        default=[],
        help="restrict to a task id (repeatable)",
    )

    bench_report = bench_sub.add_parser(
        "report", help="aggregate bench scores from a finished run"
    )
    bench_report.add_argument("run_dir", help="Roller run directory")
    bench_report.add_argument(
        "--output", "-o", help="also write the report JSON to this path"
    )

    collect = subparsers.add_parser("collect", help="collect trajectories")
    collect.add_argument("--tasks", required=True, help="JSON or JSONL task list")
    collect.add_argument(
        "--model",
        action="append",
        default=[],
        help="model identifier; repeat to run every task on multiple models",
    )
    collect.add_argument(
        "--harness",
        choices=HARNESS_NAMES,
        default=None,
        help="default/override task-agent harness (task 'harness' is used when omitted)",
    )
    collect.add_argument("--prompt-key", default="prompt")
    collect.add_argument("--id-key", default="id")
    collect.add_argument("--workspace", help="default host workspace copied into each container")
    collect.add_argument("--timeout", type=int, default=600, help="per-trajectory timeout in seconds")
    collect.add_argument("--parallel", type=int, default=1)
    collect.add_argument("--retries", type=int, default=0)
    collect.add_argument("--start-index", type=int, default=0, help="first normalized task index")
    collect.add_argument("--limit", type=int, help="maximum number of tasks")
    collect.add_argument("--output", default="runs", help="root directory for collections")
    collect.add_argument("--run-name", help="run directory name; defaults to a UTC timestamp")
    collect.add_argument("--resume", action="store_true", help="skip completed task/model pairs")
    collect.add_argument("--dry-run", action="store_true", help="validate and print the execution plan")
    collect.add_argument(
        "--max-input-file-bytes",
        type=int,
        default=10 * 1024 * 1024 * 1024,
        help="maximum size of one snapshotted input/context file",
    )
    collect.add_argument(
        "--max-task-input-bytes",
        type=int,
        default=50 * 1024 * 1024 * 1024,
        help="maximum total input bytes for one task",
    )
    collect.add_argument(
        "--task-timeouts",
        action="store_true",
        help=(
            "honor each task row's own 'timeout_seconds', falling back to "
            "--timeout when a row does not declare one (used by bench runs)"
        ),
    )
    collect.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )

    runtime = collect.add_argument_group("Harness runtime and trajectory")
    runtime.add_argument(
        "--docker-image",
        help=(
            "harness image; defaults to the corresponding ROLLER_*_IMAGE "
            "environment variable or Roller's pinned Harness image"
        ),
    )
    runtime.add_argument("--openclaw-image", help="OpenClaw image override (for mixed Harness runs)")
    runtime.add_argument("--codex-image", help="Codex image override (for mixed Harness runs)")
    runtime.add_argument("--hermes-image", help="Hermes image override (for mixed Harness runs)")
    runtime.add_argument("--opencode-image", help="OpenCode image override (for mixed Harness runs)")
    runtime.add_argument("--deepseek-image", help="DeepSeek image override (for mixed Harness runs)")
    runtime.add_argument("--claude-code-image", help="Claude Code image override (for mixed Harness runs)")
    runtime.add_argument(
        "--container-user",
        default="0",
        help="Docker user for the disposable task container (default: root); use 'image' for the image default",
    )
    runtime.add_argument("--docker-binary", default="docker")
    runtime.add_argument("--network", default="host")
    runtime.add_argument(
        "--thinking",
        type=normalize_thinking,
        choices=THINKING_LEVELS,
        metavar="LEVEL",
        help=(
            "Harness reasoning/thinking level; 'none' aliases 'off'. Each Harness "
            "applies its own validation and effort mapping"
        ),
    )
    runtime.add_argument(
        "--external-thinking",
        choices=("off", "external", "external_always"),
        default="off",
        help=(
            "tool-based CoT for OpenAI Responses in OpenClaw or Codex: external "
            "forces the first deep_think call; external_always pairs every action "
            "with deep_think; Claude Code, Hermes and OpenCode currently accept only 'off'"
        ),
    )
    runtime.add_argument(
        "--pass-env",
        action="append",
        default=[],
        metavar="NAME",
        help="additional host environment variable to pass into the Harness container",
    )
    runtime.add_argument(
        "--wire-trace",
        choices=("off", "actf", "full"),
        default="actf",
        help=(
            "wire-level recording through a local reverse proxy: 'actf' (default) embeds "
            "the tools schema into trace_ACTF.json and keeps no extra file, 'full' "
            "additionally saves wire_trace.jsonl with every request body, 'off' disables "
            "(OpenClaw uses model-config baseUrl; Codex uses --api-base; external "
            "thinking always uses an internal protocol proxy)"
        ),
    )
    runtime.add_argument(
        "--keep-thinking",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "keep visible reasoning in ACTF/SFT. For OpenClaw, --no-keep-thinking also "
            "strips reasoning from request history at the wire layer; for Codex and "
            "Claude Code/Hermes/OpenCode it controls export and leaves native raw/resume context unchanged"
        ),
    )
    runtime.add_argument("--keep-container-on-error", action="store_true")

    openclaw = collect.add_argument_group("OpenClaw")
    openclaw.add_argument("--model-config", help="OpenClaw models JSON; supports ${ENV_NAME}")
    openclaw.add_argument("--gateway-port", type=int, default=18789)
    openclaw.add_argument("--gateway-port-step", type=int, default=10)
    openclaw.add_argument("--gateway-startup-timeout", type=float, default=30.0)
    openclaw.add_argument("--session-id", default="roller")
    openclaw.add_argument("--search-provider")
    openclaw.add_argument(
        "--collect-path",
        action="append",
        default=[],
        metavar="CONTAINER_PATH",
        help="legacy: copy an extra container path into results/legacy",
    )
    openclaw.add_argument(
        "--set-image-model",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    openclaw.add_argument(
        "--cache-trace",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="record the assembled system prompt per LLM call (diagnostics.cacheTrace)",
    )

    api = collect.add_argument_group("Codex, Claude Code, Hermes and OpenCode API")
    api.add_argument(
        "--api-base",
        "--anthropic-base-url",
        dest="api_base",
        help=(
            "model API base: OpenAI-compatible base for Codex/Hermes/OpenCode or Anthropic base/"
            "full /v1/messages endpoint for Claude Code; defaults to the Harness env"
        ),
    )
    api.add_argument("--hermes-api-base", help="Hermes-only OpenAI-compatible Chat Completions API base")
    api.add_argument("--hermes-api-key-env", help="Hermes-only API key environment variable")
    api.add_argument("--opencode-api-base", help="OpenCode-only Chat Completions API base")
    api.add_argument("--deepseek-api-base", help="DeepSeek-only Chat Completions API base")
    api.add_argument("--opencode-api-key-env", help="OpenCode-only API key environment variable")
    api.add_argument("--deepseek-api-key-env", help="DeepSeek-only API key environment variable")
    api.add_argument("--codex-api-base", help="Codex-only API base override (for mixed Harness runs)")
    api.add_argument("--claude-api-base", help="Claude Code-only API base override (for mixed Harness runs)")
    api.add_argument(
        "--api-key-env",
        help=(
            "host environment variable containing the model API key; defaults to "
            "OPENAI_API_KEY for Codex/Hermes/OpenCode and ANTHROPIC_API_KEY for Claude Code"
        ),
    )
    api.add_argument("--codex-api-key-env", help="Codex-only API key environment variable")
    api.add_argument("--claude-api-key-env", help="Claude Code-only API key environment variable")

    hermes = collect.add_argument_group("Hermes")
    hermes.add_argument("--hermes-max-iterations", type=int, default=12, help="maximum model iterations per Hermes user turn (default: 12)")
    hermes.add_argument("--hermes-context-length", type=int, help="override Hermes model context window (at least 64000 tokens)")

    opencode = collect.add_argument_group("OpenCode")
    opencode.add_argument("--opencode-max-steps", type=int, default=12, help="maximum model steps per OpenCode user turn (default: 12)")
    opencode.add_argument("--opencode-context-length", type=int, default=128000, help="OpenCode model context window (default: 128000 tokens)")
    opencode.add_argument("--opencode-output-tokens", type=int, default=8192, help="OpenCode maximum output tokens (default: 8192)")

    deepseek = collect.add_argument_group("DeepSeek")
    deepseek.add_argument("--deepseek-max-steps", type=int, default=12, help="maximum model steps per DeepSeek user turn (default: 12)")
    deepseek.add_argument("--deepseek-context-length", type=int, default=128000, help="DeepSeek model context window (default: 128000 tokens)")
    deepseek.add_argument("--deepseek-output-tokens", type=int, default=8192, help="DeepSeek maximum output tokens (default: 8192)")

    codex = collect.add_argument_group("Codex")
    codex.add_argument(
        "--codex-config",
        help="optional Codex config.toml copied into the disposable container",
    )
    codex.add_argument(
        "--reasoning-summary",
        choices=("auto", "concise", "detailed", "none"),
        help="Codex model_reasoning_summary override",
    )
    codex.add_argument(
        "--codex-web-search",
        choices=("disabled", "cached", "live"),
        default="disabled",
    )
    codex.add_argument(
        "--codex-websockets",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "allow the custom Codex provider to use Responses WebSockets; "
            "disabled by default for OpenAI-compatible HTTP proxies"
        ),
    )

    claude = collect.add_argument_group("Claude Code")
    claude.add_argument(
        "--claude-settings",
        help="optional Claude Code settings.json copied into the disposable container",
    )
    claude.add_argument(
        "--claude-max-turns",
        type=int,
        help="optional Claude Code --max-turns limit for each predefined user message",
    )
    claude.add_argument(
        "--claude-max-thinking-tokens",
        type=int,
        help="optional MAX_THINKING_TOKENS value passed to Claude Code",
    )
    claude.add_argument(
        "--claude-signature-recovery",
        choices=("off", "fill", "prefer"),
        default="off",
        help=(
            "experimental post-run replay of signed Claude thinking: 'fill' only "
            "fills missing reasoning, 'prefer' replaces native summaries, 'off' disables"
        ),
    )
    claude.add_argument(
        "--claude-signature-recovery-attempts",
        type=int,
        default=1,
        help="maximum replay attempts per signed thinking block (default: 1)",
    )
    claude.add_argument(
        "--claude-signature-recovery-max-tokens",
        type=int,
        default=8192,
        help="maximum visible tokens for each signature replay request (default: 8192)",
    )
    claude.add_argument(
        "--claude-permission-mode",
        choices=CLAUDE_PERMISSION_MODES,
        default="bypassPermissions",
    )
    return parser


def _deduplicate(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _docker_preflight(binary: str, image: str) -> str:
    if shutil.which(binary) is None:
        raise RuntimeError(f"Docker executable not found: {binary}")
    try:
        result = subprocess.run(
            [binary, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Docker preflight failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Docker daemon is unavailable: {detail}")
    try:
        inspected = subprocess.run(
            [binary, "image", "inspect", image, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Docker image preflight failed for {image}: {exc}") from exc
    image_digest = inspected.stdout.strip()
    if inspected.returncode != 0 or not image_digest:
        detail = (inspected.stderr or inspected.stdout).strip()
        raise RuntimeError(
            f"Docker image is not available locally: {image}; pull or load it first"
            + (f" ({detail})" if detail else "")
        )
    return image_digest


def _run_name(harness: str, value: str | None) -> str:
    if value:
        return safe_component(value, max_length=100)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{harness}_{timestamp}"


def _dry_run_payload(args: argparse.Namespace, requests: list) -> dict:
    cli_harness = args._cli_harness if hasattr(args, "_cli_harness") else args.harness
    agent_harnesses = {
        cli_harness or getattr(request.task, "harness", "openclaw")
        for request in requests
    }
    return {
        "dry_run": True,
        "harness": next(iter(agent_harnesses)) if len(agent_harnesses) == 1 else "mixed",
        "docker_image": args.docker_image,
        "tasks": len({request.task.id for request in requests}),
        "models": list(dict.fromkeys(request.model for request in requests)),
        "trajectories": len(requests),
        "parallel": args.parallel,
        "timeout_seconds": args.timeout,
        "plan": [
            {
                "request_index": request.index,
                "task_id": request.task.id,
                "harness": cli_harness or request.task.harness,
                "model": request.model,
                "turns": (
                    {
                        "mode": "user_simulator",
                        "max": request.task.user_simulator.max_turns,
                    }
                    if request.task.user_simulator is not None
                    else {"mode": "predefined", "count": len(request.task.prompts)}
                ),
                "user_simulator": (
                    {
                        "harness": request.task.user_simulator.harness,
                        "model": request.task.user_simulator.model or request.model,
                        "max_turns": request.task.user_simulator.max_turns,
                    }
                    if request.task.user_simulator is not None
                    else None
                ),
                "workspace": str(request.task.workspace) if request.task.workspace else None,
                "inputs": len(request.task.inputs),
                "context_files": len(request.task.context_files),
                "output_dir": request.task.output_dir,
                "output_collection": "all",
                "legacy_workspace": request.task.legacy_workspace,
            }
            for request in requests
        ],
    }


def _with_task_timeout(request):
    """Apply a task's own ``timeout_seconds`` when the row declares one.

    The request already carries the global ``--timeout``, so a row without its
    own value is returned untouched.
    """

    raw = getattr(request.task, "raw", None)
    declared = raw.get("timeout_seconds") if isinstance(raw, dict) else None
    if isinstance(declared, bool) or not isinstance(declared, int) or declared <= 0:
        return request
    if declared == request.timeout_seconds:
        return request
    return dataclasses.replace(request, timeout_seconds=declared)


def _collect(args: argparse.Namespace) -> int:
    if args.parallel < 1:
        raise ValueError("--parallel must be at least 1")
    if args.retries < 0:
        raise ValueError("--retries cannot be negative")
    if args.timeout <= 0:
        raise ValueError("--timeout must be greater than zero")
    if args.start_index < 0:
        raise ValueError("--start-index cannot be negative")
    if args.limit is not None and args.limit <= 0:
        raise ValueError("--limit must be greater than zero")
    bundle_policy = BundlePolicy(
        max_file_bytes=args.max_input_file_bytes,
        max_task_input_bytes=args.max_task_input_bytes,
    )

    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else None
    if workspace is not None and not workspace.is_dir():
        raise FileNotFoundError(f"workspace is not a directory: {workspace}")
    tasks_path = Path(args.tasks).expanduser().resolve()
    tasks = load_tasks(
        tasks_path,
        prompt_key=args.prompt_key,
        id_key=args.id_key,
        global_workspace=workspace,
    )
    end_index = args.start_index + args.limit if args.limit is not None else None
    tasks = tasks[args.start_index:end_index]
    requests = build_requests(tasks, args.model, args.timeout)
    if not requests:
        raise TaskFormatError("task list contains no tasks")
    if getattr(args, "task_timeouts", False):
        # Benches size their own timeouts per task; a single global value would
        # either cut long tasks short or let short ones idle.
        requests = [_with_task_timeout(request) for request in requests]

    agent_harnesses = {
        args.harness or getattr(request.task, "harness", "openclaw")
        for request in requests
    }
    simulator_harnesses = {
        request.task.user_simulator.harness
        for request in requests
        if request.task.user_simulator is not None
    }
    selected_harnesses = agent_harnesses | simulator_harnesses
    cli_harness = args.harness
    args._cli_harness = cli_harness
    # Resolve the historical global default after loading tasks.  This keeps
    # ``--harness`` backward compatible while allowing a task-level harness.
    args.harness = next(name for name in HARNESS_NAMES if name in agent_harnesses)
    requested_api_base = args.api_base
    if args.api_base is None:
        if selected_harnesses == {"codex"}:
            args.api_base = os.environ.get("OPENAI_BASE_URL")
        elif selected_harnesses == {"claude-code"}:
            args.api_base = os.environ.get("ANTHROPIC_BASE_URL")
    hermes_api_base = args.hermes_api_base or requested_api_base or os.environ.get("OPENAI_BASE_URL")
    opencode_api_base = args.opencode_api_base or requested_api_base or os.environ.get("OPENAI_BASE_URL")
    deepseek_api_base = args.deepseek_api_base or requested_api_base or os.environ.get("DEEPSEEK_BASE_URL")
    codex_api_base = args.codex_api_base or requested_api_base or os.environ.get("OPENAI_BASE_URL")
    claude_api_base = args.claude_api_base or requested_api_base or os.environ.get("ANTHROPIC_BASE_URL")
    requested_api_key_env = args.api_key_env
    if args.api_key_env is None:
        args.api_key_env = (
            "ANTHROPIC_API_KEY" if selected_harnesses == {"claude-code"}
            else "OPENAI_API_KEY"
        )
    hermes_api_key_env = args.hermes_api_key_env or requested_api_key_env or "OPENAI_API_KEY"
    opencode_api_key_env = args.opencode_api_key_env or requested_api_key_env or "OPENAI_API_KEY"
    deepseek_api_key_env = args.deepseek_api_key_env or requested_api_key_env or "DEEPSEEK_API_KEY"
    codex_api_key_env = args.codex_api_key_env or requested_api_key_env or "OPENAI_API_KEY"
    claude_api_key_env = args.claude_api_key_env or requested_api_key_env or "ANTHROPIC_API_KEY"
    defaults = {
        "hermes": os.environ.get("ROLLER_HERMES_IMAGE", DEFAULT_HERMES_IMAGE),
        "opencode": os.environ.get("ROLLER_OPENCODE_IMAGE", DEFAULT_OPENCODE_IMAGE),
        "deepseek": os.environ.get("ROLLER_DEEPSEEK_IMAGE", DEFAULT_DEEPSEEK_IMAGE),
        "openclaw": os.environ.get("ROLLER_OPENCLAW_IMAGE", DEFAULT_OPENCLAW_IMAGE),
        "codex": os.environ.get("ROLLER_CODEX_IMAGE", DEFAULT_CODEX_IMAGE),
        "claude-code": os.environ.get("ROLLER_CLAUDE_CODE_IMAGE", DEFAULT_CLAUDE_CODE_IMAGE),
    }
    image_overrides = {
        "hermes": args.hermes_image,
        "opencode": args.opencode_image,
        "deepseek": args.deepseek_image,
        "openclaw": args.openclaw_image,
        "codex": args.codex_image,
        "claude-code": args.claude_code_image,
    }
    images = {
        name: (image_overrides[name] or args.docker_image or defaults[name])
        for name in selected_harnesses
    }
    args.docker_image = args.docker_image or images[args.harness]
    for name in HARNESS_NAMES:
        if name in selected_harnesses and args.external_thinking != "off" and not HARNESSES[name].external_thinking:
            raise ValueError(f"{HARNESSES[name].label} external thinking is not implemented; use --external-thinking off")
    if args.collect_path and "openclaw" not in selected_harnesses:
        raise ValueError("--collect-path requires an OpenClaw participant")

    model_config = None
    model_config_path: Path | None = None
    if args.model_config:
        if "openclaw" not in selected_harnesses:
            raise ValueError("--model-config is only valid when an OpenClaw participant is selected")
        model_config_path = Path(args.model_config).expanduser().resolve()
        if not model_config_path.is_file():
            raise FileNotFoundError(f"model config not found: {model_config_path}")
        model_config = load_model_config(model_config_path)

    codex_config_path: Path | None = None
    codex_config_digest: str | None = None
    if args.codex_config:
        if "codex" not in selected_harnesses:
            raise ValueError("--codex-config is only valid when a Codex participant is selected")
        codex_config_path = Path(args.codex_config).expanduser().resolve()
        if not codex_config_path.is_file():
            raise FileNotFoundError(f"Codex config not found: {codex_config_path}")
        codex_config_digest = hashlib.sha256(codex_config_path.read_bytes()).hexdigest()

    claude_settings_path: Path | None = None
    claude_settings_fingerprint: str | None = None
    if args.claude_settings:
        if "claude-code" not in selected_harnesses:
            raise ValueError("--claude-settings is only valid when a Claude Code participant is selected")
        claude_settings_path = Path(args.claude_settings).expanduser().resolve()
        if not claude_settings_path.is_file():
            raise FileNotFoundError(
                f"Claude Code settings file not found: {claude_settings_path}"
            )
        claude_settings_fingerprint = hashlib.blake2b(
            claude_settings_path.read_bytes(), digest_size=16
        ).hexdigest()
    if "claude-code" not in selected_harnesses and args.claude_signature_recovery != "off":
        raise ValueError(
            "--claude-signature-recovery is only valid when a Claude Code participant is selected"
        )

    hermes_config: HermesConfig | None = None
    opencode_config: OpenCodeConfig | None = None
    deepseek_config: DeepSeekConfig | None = None
    openclaw_config: OpenClawConfig | None = None
    codex_config: CodexConfig | None = None
    claude_code_config: ClaudeCodeConfig | None = None
    if "openclaw" in selected_harnesses:
        for request in requests:
            agent_harness = cli_harness or request.task.harness
            if agent_harness == "openclaw":
                validate_external_thinking_provider(model_config, request.model, args.external_thinking)
            spec = request.task.user_simulator
            if spec is not None and spec.harness == "openclaw":
                validate_external_thinking_provider(model_config, spec.model or request.model, args.external_thinking)
        openclaw_config = OpenClawConfig(
            docker_image=images["openclaw"],
            container_user=(
                None if args.container_user == "image" else args.container_user
            ),
            gateway_port_start=args.gateway_port,
            gateway_port_step=args.gateway_port_step,
            gateway_startup_timeout=args.gateway_startup_timeout,
            network=args.network,
            session_id=args.session_id,
            model_config=model_config,
            pass_env=_deduplicate((*OPENCLAW_COMMON_ENV_NAMES, *args.pass_env)),
            search_provider=args.search_provider,
            thinking=args.thinking,
            external_thinking=args.external_thinking,
            capture_cache_trace=args.cache_trace,
            wire_trace_mode=args.wire_trace,
            keep_thinking=args.keep_thinking,
            collect_paths=tuple(args.collect_path),
            keep_container_on_error=args.keep_container_on_error,
            set_image_model=args.set_image_model,
            docker_binary=args.docker_binary,
        )
    if "codex" in selected_harnesses:
        if args.collect_path and "openclaw" not in selected_harnesses:
            raise ValueError("--collect-path requires an OpenClaw participant")
        codex_config = CodexConfig(
            docker_image=images["codex"],
            container_user=(
                None if args.container_user == "image" else args.container_user
            ),
            network=args.network,
            config_path=codex_config_path,
            api_base=codex_api_base,
            api_key_env=codex_api_key_env,
            pass_env=_deduplicate((*CODEX_COMMON_ENV_NAMES, *args.pass_env)),
            reasoning_effort=args.thinking,
            reasoning_summary=args.reasoning_summary,
            web_search=args.codex_web_search,
            supports_websockets=args.codex_websockets,
            external_thinking=args.external_thinking,
            wire_trace_mode=args.wire_trace,
            keep_thinking=args.keep_thinking,
            keep_container_on_error=args.keep_container_on_error,
            docker_binary=args.docker_binary,
        )
    if "claude-code" in selected_harnesses:
        if args.collect_path and "openclaw" not in selected_harnesses:
            raise ValueError("--collect-path requires an OpenClaw participant")
        claude_code_config = ClaudeCodeConfig(
            docker_image=images["claude-code"],
            container_user=(
                None if args.container_user == "image" else args.container_user
            ),
            network=args.network,
            settings_path=claude_settings_path,
            settings_fingerprint=claude_settings_fingerprint,
            api_base=claude_api_base,
            api_key_env=claude_api_key_env,
            pass_env=_deduplicate(
                (*CLAUDE_CODE_COMMON_ENV_NAMES, *args.pass_env)
            ),
            reasoning_effort=args.thinking,
            max_thinking_tokens=args.claude_max_thinking_tokens,
            max_turns=args.claude_max_turns,
            permission_mode=args.claude_permission_mode,
            wire_trace_mode=args.wire_trace,
            keep_thinking=args.keep_thinking,
            signature_recovery=args.claude_signature_recovery,
            signature_recovery_attempts=args.claude_signature_recovery_attempts,
            signature_recovery_max_tokens=args.claude_signature_recovery_max_tokens,
            keep_container_on_error=args.keep_container_on_error,
            docker_binary=args.docker_binary,
        )
        for request in requests:
            if (cli_harness or request.task.harness) == "claude-code":
                claude_effort(resolve_thinking(request.task.thinking, claude_code_config.reasoning_effort))
            spec = request.task.user_simulator
            if spec is not None and spec.harness == "claude-code":
                claude_effort(resolve_thinking(spec.thinking, claude_code_config.reasoning_effort))

    if "hermes" in selected_harnesses:
        hermes_config = HermesConfig(
            docker_image=images["hermes"],
            container_user=None if args.container_user == "image" else args.container_user,
            network=args.network, api_base=hermes_api_base, api_key_env=hermes_api_key_env,
            pass_env=_deduplicate((*HERMES_COMMON_ENV_NAMES, *args.pass_env)),
            reasoning_effort=args.thinking, max_iterations=args.hermes_max_iterations,
            context_length=args.hermes_context_length,
            wire_trace_mode=args.wire_trace, keep_thinking=args.keep_thinking,
            keep_container_on_error=args.keep_container_on_error, docker_binary=args.docker_binary,
        )
        for request in requests:
            if (cli_harness or request.task.harness) == "hermes":
                hermes_effort(resolve_thinking(request.task.thinking, hermes_config.reasoning_effort))
            spec = request.task.user_simulator
            if spec is not None and spec.harness == "hermes":
                hermes_effort(resolve_thinking(spec.thinking, hermes_config.reasoning_effort))

    if "opencode" in selected_harnesses:
        opencode_config = OpenCodeConfig(
            docker_image=images["opencode"],
            container_user=None if args.container_user == "image" else args.container_user,
            network=args.network, api_base=opencode_api_base, api_key_env=opencode_api_key_env,
            pass_env=_deduplicate((*OPENCODE_COMMON_ENV_NAMES, *args.pass_env)),
            reasoning_effort=args.thinking, max_steps=args.opencode_max_steps,
            context_length=args.opencode_context_length, output_tokens=args.opencode_output_tokens,
            wire_trace_mode=args.wire_trace, keep_thinking=args.keep_thinking,
            keep_container_on_error=args.keep_container_on_error, docker_binary=args.docker_binary,
        )
        for request in requests:
            if (cli_harness or request.task.harness) == "opencode":
                opencode_effort(resolve_thinking(request.task.thinking, opencode_config.reasoning_effort))
            spec = request.task.user_simulator
            if spec is not None and spec.harness == "opencode":
                opencode_effort(resolve_thinking(spec.thinking, opencode_config.reasoning_effort))

    if "deepseek" in selected_harnesses:
        deepseek_config = DeepSeekConfig(
            docker_image=images["deepseek"],
            container_user=None if args.container_user == "image" else args.container_user,
            network=args.network, api_base=deepseek_api_base, api_key_env=deepseek_api_key_env,
            pass_env=_deduplicate((*DEEPSEEK_COMMON_ENV_NAMES, *args.pass_env)),
            reasoning_effort=args.thinking, max_steps=args.deepseek_max_steps,
            context_length=args.deepseek_context_length, output_tokens=args.deepseek_output_tokens,
            wire_trace_mode=args.wire_trace, keep_thinking=args.keep_thinking,
            keep_container_on_error=args.keep_container_on_error, docker_binary=args.docker_binary,
        )
        for request in requests:
            if (cli_harness or request.task.harness) == "deepseek":
                deepseek_effort(resolve_thinking(request.task.thinking, deepseek_config.reasoning_effort))
            spec = request.task.user_simulator
            if spec is not None and spec.harness == "deepseek":
                deepseek_effort(resolve_thinking(spec.thinking, deepseek_config.reasoning_effort))

    if args.dry_run:
        # Exercise all path, hash, collision and size checks without leaving a run directory.
        with tempfile.TemporaryDirectory(prefix="roller-dry-run-") as temporary:
            TaskBundleStore(Path(temporary), bundle_policy).prepare_all(tasks)
        print(json.dumps(_dry_run_payload(args, requests), ensure_ascii=False, indent=2))
        return 0

    docker_image_digests = {
        harness: _docker_preflight(args.docker_binary, image)
        for harness, image in images.items()
    }
    run_label = next(iter(agent_harnesses)) if len(agent_harnesses) == 1 else "mixed"
    output_root = Path(args.output).expanduser().resolve()
    run_dir = output_root / _run_name(run_label, args.run_name)
    existing_run = run_dir.exists() and any(run_dir.iterdir())
    if run_dir.exists() and not args.resume:
        raise FileExistsError(f"run directory already exists; use --resume or another --run-name: {run_dir}")
    output_root.mkdir(parents=True, exist_ok=True)

    previous_info: dict[str, object] = {}
    previous_info_path = run_dir / "info.json"
    if args.resume and previous_info_path.is_file():
        try:
            candidate = json.loads(previous_info_path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                previous_info = candidate
        except (OSError, json.JSONDecodeError):
            previous_info = {}

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    harness_configurations: dict[str, dict[str, object]] = {}
    if openclaw_config is not None:
        harness_configurations["openclaw"] = {
            "docker_image": images["openclaw"],
            "container_user": args.container_user,
            "model_config_path": (
                str(model_config_path) if model_config_path else None
            ),
            "gateway_port_start": args.gateway_port,
            "gateway_port_step": args.gateway_port_step,
            "network": args.network,
            "session_id": args.session_id,
            "search_provider": args.search_provider,
            "thinking": args.thinking,
            "external_thinking": args.external_thinking,
            "keep_thinking": args.keep_thinking,
            "cache_trace": args.cache_trace,
            "wire_trace": args.wire_trace,
            "pass_env_names": list(openclaw_config.pass_env),
            "collect_paths": args.collect_path,
        }
    if codex_config is not None:
        harness_configurations["codex"] = {
            "docker_image": images["codex"],
            "container_user": args.container_user,
            "config_path": str(codex_config_path) if codex_config_path else None,
            "config_sha256": codex_config_digest,
            "network": args.network,
            "api_base": redact_api_base(codex_api_base) if codex_api_base else None,
            "api_key_env": codex_api_key_env,
            "reasoning_effort": args.thinking,
            "reasoning_summary": args.reasoning_summary,
            "external_thinking": args.external_thinking,
            "wire_trace": args.wire_trace,
            "keep_thinking": args.keep_thinking,
            "web_search": args.codex_web_search,
            "supports_websockets": args.codex_websockets,
            "pass_env_names": list(codex_config.pass_env),
        }
    if claude_code_config is not None:
        harness_configurations["claude-code"] = {
            "docker_image": images["claude-code"],
            "container_user": args.container_user,
            "settings_path": (
                str(claude_settings_path) if claude_settings_path else None
            ),
            "settings_fingerprint": claude_settings_fingerprint,
            "network": args.network,
            "api_base": (
                redact_anthropic_base_url(claude_api_base)
                if claude_api_base
                else None
            ),
            "api_key_env": claude_api_key_env,
            "reasoning_effort": args.thinking,
            "max_thinking_tokens": args.claude_max_thinking_tokens,
            "max_turns": args.claude_max_turns,
            "permission_mode": args.claude_permission_mode,
            "external_thinking": "off",
            "wire_trace": args.wire_trace,
            "keep_thinking": args.keep_thinking,
            "signature_recovery": args.claude_signature_recovery,
            "signature_recovery_attempts": args.claude_signature_recovery_attempts,
            "signature_recovery_max_tokens": (
                args.claude_signature_recovery_max_tokens
            ),
            "pass_env_names": list(claude_code_config.pass_env),
        }

    if hermes_config is not None:
        harness_configurations["hermes"] = {
            "docker_image": images["hermes"], "container_user": args.container_user,
            "network": args.network, "api_base": hermes_api_base or "https://api.openai.com/v1",
            "api_key_env": hermes_api_key_env, "reasoning_effort": args.thinking,
            "max_iterations": args.hermes_max_iterations, "hermes_revision": HERMES_REVISION,
            "context_length": args.hermes_context_length,
            "toolsets": ["terminal", "file"], "api_mode": "chat_completions",
            "external_thinking": "off", "wire_trace": args.wire_trace,
            "keep_thinking": args.keep_thinking, "pass_env_names": list(hermes_config.pass_env),
        }

    if opencode_config is not None:
        harness_configurations["opencode"] = {
            "docker_image": images["opencode"], "container_user": args.container_user,
            "network": args.network, "api_base": opencode_api_base or "https://api.openai.com/v1",
            "api_key_env": opencode_api_key_env, "reasoning_effort": args.thinking,
            "max_steps": args.opencode_max_steps, "opencode_version": OPENCODE_VERSION,
            "context_length": args.opencode_context_length, "output_tokens": args.opencode_output_tokens,
            "tools": list(OPENCODE_TOOLS), "api_mode": "chat_completions",
            "external_thinking": "off", "wire_trace": args.wire_trace,
            "keep_thinking": args.keep_thinking, "pass_env_names": list(opencode_config.pass_env),
            "compaction": {"auto": False, "prune": False},
        }

    if deepseek_config is not None:
        harness_configurations["deepseek"] = {
            "docker_image": images["deepseek"], "container_user": args.container_user,
            "network": args.network, "api_base": deepseek_api_base or "https://api.deepseek.com",
            "api_key_env": deepseek_api_key_env, "reasoning_effort": args.thinking,
            "max_steps": args.deepseek_max_steps, "deepseek_version": DEEPSEEK_VERSION,
            "context_length": args.deepseek_context_length, "output_tokens": args.deepseek_output_tokens,
            "tools": list(DEEPSEEK_TOOLS), "api_mode": "chat_completions",
            "external_thinking": "off", "wire_trace": args.wire_trace,
            "keep_thinking": args.keep_thinking, "pass_env_names": list(deepseek_config.pass_env),
            "profile": "sdk-minimal", "retry_count": 0,
        }

    run_info = {
        "schema_version": "roller.run/v1",
        "roller_version": __version__,
        "configuration": {
            "harness": run_label,
            "agent_harnesses": sorted(agent_harnesses),
            "simulator_harnesses": sorted(simulator_harnesses),
            "tasks": str(tasks_path),
            "models": args.model,
            "trajectory_count": len(requests),
            "workspace": str(workspace) if workspace else None,
            "timeout_seconds": args.timeout,
            "parallel": args.parallel,
            "retries": args.retries,
            "start_index": args.start_index,
            "limit": args.limit,
            "resume": args.resume,
            "task_schema": "roller.task/v1",
            "conversation": {
                "user_simulator_tasks": sum(
                    task.user_simulator is not None for task in tasks
                ),
                "simulator_gateway_slot_offset": (
                    args.parallel if "openclaw" in simulator_harnesses else None
                ),
            },
            "bundle_policy": {
                "max_input_file_bytes": args.max_input_file_bytes,
                "max_task_input_bytes": args.max_task_input_bytes,
            },
            **harness_configurations,
        },
        "state": "initializing",
        "started_at": previous_info.get("started_at", now),
    }
    if args.resume and existing_run:
        previous_count = previous_info.get("resume_count", 0)
        run_info["last_resumed_at"] = now
        run_info["resume_count"] = (
            previous_count + 1
            if isinstance(previous_count, int) and not isinstance(previous_count, bool)
            else 1
        )

    results: list[dict] | None = None
    with tempfile.TemporaryDirectory(prefix=".roller-bundle-", dir=output_root) as temporary:
        bundle_store = TaskBundleStore(Path(temporary), bundle_policy)
        try:
            bundles = bundle_store.prepare_all(tasks)
            requests = bundle_store.attach(requests, bundles)
            run_dir.mkdir(parents=True, exist_ok=True)
            run_lock = build_run_lock(
                requests,
                roller_version=__version__,
                execution={
                    "harness": run_label,
                    "agent_harnesses": sorted(agent_harnesses),
                    "simulator_harnesses": sorted(simulator_harnesses),
                    "task_schema": "roller.task/v1",
                    "output_collection": "all",
                    **{
                        name: {
                            **payload,
                            "docker_image": images[name],
                            "docker_image_digest": docker_image_digests[name],
                        }
                        for name, payload in harness_configurations.items()
                    },
                },
                model_config=model_config if "openclaw" in selected_harnesses else None,
            )
            lock_decision = ensure_run_lock(
                run_dir / "lock.json",
                run_lock,
                resume=args.resume,
                existing_run=existing_run,
            )
            if lock_decision.legacy_run:
                logger.warning(
                    "legacy run has not completed trusted lock adoption; rerunning "
                    "all samples before enabling resume"
                )
            configuration = run_info["configuration"]
            if isinstance(configuration, dict):
                configuration["resume_reuse_enabled"] = (
                    lock_decision.reuse_existing_samples
                )
                for name, digest in docker_image_digests.items():
                    harness_info = configuration.get(name)
                    if isinstance(harness_info, dict):
                        harness_info["docker_image_digest"] = digest
            run_info["lock"] = {
                "path": "lock.json",
                "schema_version": RUN_LOCK_SCHEMA,
                "legacy_adopted": lock_decision.legacy_run,
            }
            atomic_write_json(run_dir / "info.json", run_info)
            keep_aux_traces = args.wire_trace == "full"

            def make_harness(name: str, slot: int):
                if name == "openclaw":
                    assert openclaw_config is not None
                    return OpenClawHarness(openclaw_config, slot)
                if name == "codex":
                    assert codex_config is not None
                    return CodexHarness(codex_config, slot)
                if name == "claude-code":
                    assert claude_code_config is not None
                    return ClaudeCodeHarness(claude_code_config, slot)
                if name == "hermes":
                    assert hermes_config is not None
                    return HermesHarness(hermes_config, slot)
                if name == "opencode":
                    assert opencode_config is not None
                    return OpenCodeHarness(opencode_config, slot)
                if name == "deepseek":
                    assert deepseek_config is not None
                    return DeepSeekHarness(deepseek_config, slot)
                raise ValueError(f"unknown Harness: {name}")

            def harness_factory(slot: int, request):
                agent = make_harness(cli_harness or request.task.harness, slot)
                binding = binding_from_task_row(getattr(request.task, "raw", None) or {})
                if binding is not None and binding.grade is not None:
                    # Grading has to read the container the agent just wrote to,
                    # so it runs inside the session rather than after teardown.
                    return GradedHarness(agent, binding)
                spec = request.task.user_simulator
                if spec is None:
                    return agent
                simulator_slot = (
                    slot + args.parallel if spec.harness == "openclaw" else slot
                )
                simulator = make_harness(spec.harness, simulator_slot)
                return ConversationManager(
                    agent,
                    simulator,
                    keep_aux_traces=keep_aux_traces,
                )
            collector = Collector(
                CollectionConfig(
                    run_dir=run_dir,
                    parallel=args.parallel,
                    retries=args.retries,
                    resume=lock_decision.reuse_existing_samples,
                    keep_aux_traces=keep_aux_traces,
                    capture_cache_trace=(
                        args.cache_trace if "openclaw" in selected_harnesses else False
                    ),
                    keep_thinking=args.keep_thinking,
                ),
                harness_factory=harness_factory,
            )
            results = collector.collect(requests)
            if lock_decision.legacy_run:
                complete_legacy_adoption(run_dir / "lock.json")
        finally:
            bundle_store.cleanup_runtime()
    assert results is not None
    failed = sum(result.get("status") in {"error", "timeout"} for result in results)
    print(f"Collected {len(results)} trajectories ({failed} failed).")
    print(f"Run directory: {run_dir}")
    print(f"Info: {run_dir / 'info.json'}")
    return 1 if failed else 0


def _bench(args: argparse.Namespace) -> int:
    if args.bench_command == "list":
        for adapter in list_benches():
            print(f"{adapter.name:<16} {adapter.description}")
        return 0
    if args.bench_command == "export":
        adapter = get_bench(args.name)
        root = Path(args.root).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"bench root is not a directory: {root}")
        rows = adapter.load(
            root,
            categories=args.category or None,
            task_ids=args.task_id or None,
        )
        if not rows:
            raise TaskFormatError(f"{adapter.name}: selection matched no tasks")
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        categories = sorted({row["bench"].get("category") or "" for row in rows} - {""})
        print(f"Exported {len(rows)} {adapter.name} tasks to {output}")
        if categories:
            print(f"Categories: {', '.join(categories)}")
        print("Run with: roller collect --tasks "
              f"{output} --model <model> --task-timeouts")
        return 0
    return _bench_report(args)


def _bench_report(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    samples_root = run_dir / "samples"
    if not samples_root.is_dir():
        raise FileNotFoundError(f"run directory has no samples/: {run_dir}")
    rows: list[dict[str, Any]] = []
    bench_names: set[str] = set()
    for info_path in sorted(samples_root.glob("*/*/info.json")):
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("skipping unreadable sample info: %s", info_path)
            continue
        if not isinstance(info, dict):
            continue
        runtime = info.get("runtime")
        payload = runtime.get("bench") if isinstance(runtime, dict) else None
        if not isinstance(payload, dict):
            continue
        name = payload.get("bench")
        if isinstance(name, str) and name:
            bench_names.add(name)
        rows.append(
            {
                "task_id": info.get("task_id"),
                "model": info.get("model"),
                "status": info.get("status"),
                "bench_scores": payload.get("scores"),
                "bench_score_key": payload.get("score_key"),
                "bench_category": payload.get("category"),
            }
        )
    if not rows:
        raise TaskFormatError(f"no graded bench samples found under {samples_root}")
    label = ", ".join(sorted(bench_names)) or "unknown"
    try:
        report = get_bench(sorted(bench_names)[0]).aggregate(rows) if len(
            bench_names
        ) == 1 else mean_aggregate(rows)
    except KeyError:
        report = mean_aggregate(rows)
    print(format_report(label, report))
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(output, report)
        print(f"Report written to {output}")
    return 0


def _view(args: argparse.Namespace) -> int:
    return serve_viewer(Path(args.run_dir), host=args.host, port=args.port)


def main(argv: Sequence[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, getattr(args, "log_level", "INFO")),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        if args.command == "collect":
            exit_code = _collect(args)
        elif args.command == "bench":
            exit_code = _bench(args)
        elif args.command == "view":
            exit_code = _view(args)
        else:
            parser.error(f"unsupported command: {args.command}")
            return
    except (FileNotFoundError, FileExistsError, TaskFormatError, ValueError, RuntimeError) as exc:
        logger.error("%s", exc)
        exit_code = 2
    raise SystemExit(exit_code)
