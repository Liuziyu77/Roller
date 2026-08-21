from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from roller.collector import CollectionConfig, Collector
from roller.bundles import BundlePolicy, TaskBundleStore
from roller.conversation import ConversationManager
from roller.harnesses.openclaw import (
    COMMON_ENV_NAMES,
    DEFAULT_OPENCLAW_IMAGE,
    OpenClawConfig,
    OpenClawHarness,
    load_model_config,
)
from roller.storage import atomic_write_json, safe_component
from roller.tasks import TaskFormatError, build_requests, load_tasks
from roller.thinking import THINKING_LEVELS, normalize_thinking
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

    collect = subparsers.add_parser("collect", help="collect trajectories")
    collect.add_argument("--tasks", required=True, help="JSON or JSONL task list")
    collect.add_argument(
        "--model",
        action="append",
        default=[],
        help="model identifier; repeat to run every task on multiple models",
    )
    collect.add_argument("--harness", choices=("openclaw",), default="openclaw")
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
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )

    openclaw = collect.add_argument_group("OpenClaw")
    openclaw.add_argument(
        "--docker-image",
        default=os.environ.get("ROLLER_OPENCLAW_IMAGE", DEFAULT_OPENCLAW_IMAGE),
    )
    openclaw.add_argument(
        "--container-user",
        default="0",
        help="Docker user for the disposable task container (default: root); use 'image' for the image default",
    )
    openclaw.add_argument("--docker-binary", default="docker")
    openclaw.add_argument("--model-config", help="OpenClaw models JSON; supports ${ENV_NAME}")
    openclaw.add_argument("--gateway-port", type=int, default=18789)
    openclaw.add_argument("--gateway-port-step", type=int, default=10)
    openclaw.add_argument("--gateway-startup-timeout", type=float, default=30.0)
    openclaw.add_argument("--network", default="host")
    openclaw.add_argument("--session-id", default="roller")
    openclaw.add_argument("--search-provider")
    openclaw.add_argument(
        "--thinking",
        type=normalize_thinking,
        choices=THINKING_LEVELS,
        metavar="LEVEL",
        help=(
            "OpenClaw logical thinking level; 'none' is accepted as an alias for 'off', "
            "and model/provider support is validated by OpenClaw"
        ),
    )
    openclaw.add_argument(
        "--pass-env",
        action="append",
        default=[],
        metavar="NAME",
        help="additional host environment variable to pass into OpenClaw",
    )
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
    openclaw.add_argument(
        "--wire-trace",
        choices=("off", "actf", "full"),
        default="actf",
        help=(
            "wire-level recording through a local reverse proxy: 'actf' (default) embeds "
            "the tools schema into trace_ACTF.json and keeps no extra file, 'full' "
            "additionally saves wire_trace.jsonl with every request body, 'off' disables "
            "(the selected provider needs a model-config baseUrl; without one, actf "
            "falls back to tools=null while full fails)"
        ),
    )
    openclaw.add_argument(
        "--keep-thinking",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "for thinking models: keep reasoning content in the trajectory AND in the "
            "context of subsequent requests. --no-keep-thinking blanks reasoning_content "
            "in trace_ACTF.json and strips reasoning from request history at the wire "
            "layer (OpenClaw itself keeps it and has no config switch)"
        ),
    )
    openclaw.add_argument("--keep-container-on-error", action="store_true")
    return parser


def _deduplicate(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _docker_preflight(binary: str) -> None:
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


def _run_name(value: str | None) -> str:
    if value:
        return safe_component(value, max_length=100)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"openclaw_{timestamp}"


def _dry_run_payload(args: argparse.Namespace, requests: list) -> dict:
    return {
        "dry_run": True,
        "harness": args.harness,
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

    model_config = None
    model_config_path: Path | None = None
    if args.model_config:
        model_config_path = Path(args.model_config).expanduser().resolve()
        if not model_config_path.is_file():
            raise FileNotFoundError(f"model config not found: {model_config_path}")
        model_config = load_model_config(model_config_path)

    openclaw_config = OpenClawConfig(
        docker_image=args.docker_image,
        container_user=None if args.container_user == "image" else args.container_user,
        gateway_port_start=args.gateway_port,
        gateway_port_step=args.gateway_port_step,
        gateway_startup_timeout=args.gateway_startup_timeout,
        network=args.network,
        session_id=args.session_id,
        model_config=model_config,
        pass_env=_deduplicate((*COMMON_ENV_NAMES, *args.pass_env)),
        search_provider=args.search_provider,
        thinking=args.thinking,
        capture_cache_trace=args.cache_trace,
        wire_trace_mode=args.wire_trace,
        keep_thinking=args.keep_thinking,
        collect_paths=tuple(args.collect_path),
        keep_container_on_error=args.keep_container_on_error,
        set_image_model=args.set_image_model,
        docker_binary=args.docker_binary,
    )

    if args.dry_run:
        # Exercise all path, hash, collision and size checks without leaving a run directory.
        with tempfile.TemporaryDirectory(prefix="roller-dry-run-") as temporary:
            TaskBundleStore(Path(temporary), bundle_policy).prepare_all(tasks)
        print(json.dumps(_dry_run_payload(args, requests), ensure_ascii=False, indent=2))
        return 0

    _docker_preflight(args.docker_binary)
    output_root = Path(args.output).expanduser().resolve()
    run_dir = output_root / _run_name(args.run_name)
    if run_dir.exists() and not args.resume:
        raise FileExistsError(f"run directory already exists; use --resume or another --run-name: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)

    run_info = {
        "schema_version": "roller.run/v1",
        "roller_version": __version__,
        "configuration": {
            "harness": args.harness,
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
                "simulator_gateway_slot_offset": args.parallel,
            },
            "bundle_policy": {
                "max_input_file_bytes": args.max_input_file_bytes,
                "max_task_input_bytes": args.max_task_input_bytes,
            },
            "openclaw": {
                "docker_image": args.docker_image,
                "container_user": args.container_user,
                "model_config_path": str(model_config_path) if model_config_path else None,
                "gateway_port_start": args.gateway_port,
                "gateway_port_step": args.gateway_port_step,
                "network": args.network,
                "session_id": args.session_id,
                "search_provider": args.search_provider,
                "thinking": args.thinking,
                "keep_thinking": args.keep_thinking,
                "cache_trace": args.cache_trace,
                "wire_trace": args.wire_trace,
                "pass_env_names": list(openclaw_config.pass_env),
                "collect_paths": args.collect_path,
            },
        },
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    atomic_write_json(run_dir / "info.json", run_info)

    results: list[dict] | None = None
    with tempfile.TemporaryDirectory(prefix=".roller-bundle-", dir=output_root) as temporary:
        bundle_store = TaskBundleStore(Path(temporary), bundle_policy)
        try:
            bundles = bundle_store.prepare_all(tasks)
            requests = bundle_store.attach(requests, bundles)
            collector = Collector(
                CollectionConfig(
                    run_dir=run_dir,
                    parallel=args.parallel,
                    retries=args.retries,
                    resume=args.resume,
                    keep_aux_traces=args.wire_trace == "full",
                    keep_thinking=args.keep_thinking,
                ),
                harness_factory=lambda slot: ConversationManager(
                    OpenClawHarness(openclaw_config, slot),
                    OpenClawHarness(openclaw_config, slot + args.parallel),
                    keep_thinking=args.keep_thinking,
                    keep_aux_traces=args.wire_trace == "full",
                ),
            )
            results = collector.collect(requests)
        finally:
            bundle_store.cleanup_runtime()
    assert results is not None
    failed = sum(result.get("status") in {"error", "timeout"} for result in results)
    print(f"Collected {len(results)} trajectories ({failed} failed).")
    print(f"Run directory: {run_dir}")
    print(f"Info: {run_dir / 'info.json'}")
    return 1 if failed else 0


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
        elif args.command == "view":
            exit_code = _view(args)
        else:
            parser.error(f"unsupported command: {args.command}")
            return
    except (FileNotFoundError, FileExistsError, TaskFormatError, ValueError, RuntimeError) as exc:
        logger.error("%s", exc)
        exit_code = 2
    raise SystemExit(exit_code)
