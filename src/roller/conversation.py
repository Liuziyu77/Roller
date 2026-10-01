from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from roller.harnesses.base import Harness, InteractiveHarness
from roller.models import HarnessResult, RunRequest, Task, UserSimulatorSpec
from roller.sft import write_sft
from roller.trajectory_audit import record_training_audits
from roller.storage import atomic_write_json
from roller.usage import ZERO_USAGE


CONVERSATION_SCHEMA = "roller.conversation/v1"
PARTICIPANT_SCHEMA = "roller.participant/v1"


@dataclass(frozen=True)
class UserSimulatorDecision:
    action: str
    content: str | None = None
    reason: str | None = None


def parse_user_simulator_decision(content: str) -> UserSimulatorDecision:
    """Parse the simulator's strict JSON response without text heuristics."""

    try:
        payload = json.loads(content.strip())
    except json.JSONDecodeError as exc:
        raise ValueError("User Simulator returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("User Simulator response must be a JSON object")
    action = payload.get("action")
    if action == "message":
        message = payload.get("content")
        if not isinstance(message, str) or not message.strip():
            raise ValueError(
                "User Simulator action 'message' requires non-empty 'content'"
            )
        return UserSimulatorDecision("message", content=message.strip())
    if action == "finish":
        reason = payload.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("User Simulator finish reason must be a string")
        return UserSimulatorDecision(
            "finish", reason=reason.strip() if isinstance(reason, str) else None
        )
    raise ValueError("User Simulator action must be 'message' or 'finish'")


def build_user_simulator_prompt(
    spec: UserSimulatorSpec,
    transcript: list[dict[str, str]],
) -> str:
    """Build one deterministic manager-to-simulator context message."""

    state = {
        "instruction": spec.instruction,
        "rounds_completed": sum(row["role"] == "assistant" for row in transcript),
        "max_turns": spec.max_turns,
        "conversation": transcript,
    }
    return (
        "You are the User Simulator in a Roller rollout. Follow the fixed "
        "instruction and inspect the complete conversation context below. Decide "
        "whether to send the task agent another user message or end the dialogue.\n\n"
        "Return exactly one JSON object and no Markdown:\n"
        '{"action":"message","content":"next instruction"}\n'
        "or\n"
        '{"action":"finish","reason":"why the task is complete"}\n\n'
        "Conversation manager state:\n"
        + json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    )


def _combined_usage(*rows: dict[str, Any]) -> dict[str, Any]:
    totals: dict[str, Any] = dict(ZERO_USAGE)
    for row in rows:
        for key in totals:
            value = row.get(key, 0)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] += value
    totals["cost_usd"] = round(float(totals["cost_usd"]), 6)
    return totals


class ConversationManager(Harness):
    """Alternate a task Harness and a User Simulator Harness in live sessions."""

    def __init__(
        self,
        agent_harness: InteractiveHarness,
        simulator_harness: InteractiveHarness,
        *,
        keep_aux_traces: bool = False,
    ) -> None:
        self.agent_harness = agent_harness
        self.simulator_harness = simulator_harness
        self.keep_aux_traces = keep_aux_traces
        self.name = agent_harness.name

    @staticmethod
    def _simulator_request(
        request: RunRequest, spec: UserSimulatorSpec
    ) -> RunRequest:
        simulator_task = Task(
            id=f"{request.task.id}__user_simulator",
            prompts=(),
            source_index=request.task.source_index,
            raw={},
            schema_version=request.task.schema_version,
            output_dir=".roller-simulator-outputs",
            thinking=spec.thinking,
        )
        return RunRequest(
            index=request.index,
            task=simulator_task,
            model=spec.model or request.model,
            timeout_seconds=request.timeout_seconds,
        )

    @staticmethod
    def _trace_source(base: Path, value: str | None) -> Path | None:
        if not value:
            return None
        path = Path(value)
        if not path.is_absolute():
            path = base / path
        return path if path.is_file() else None

    @staticmethod
    def _archive_simulator(
        simulator_dir: Path,
        result: HarnessResult,
        model: str,
        harness: str,
        *,
        keep_aux_traces: bool,
    ) -> dict[str, Any]:
        source = ConversationManager._trace_source(simulator_dir, result.trace_path)
        trajectory_source = ConversationManager._trace_source(
            simulator_dir, result.trajectory_path
        )
        raw_path = simulator_dir / "trace.jsonl"
        actf_path = simulator_dir / "trace_ACTF.json"
        sft_path = simulator_dir / "trace_SFT.json"
        # Remove stale exports before any copy/parse can fail.
        if result.status != "ok":
            sft_path.unlink(missing_ok=True)
        if source is None and result.status == "ok":
            raise RuntimeError("User Simulator finished but no raw trace was found")
        if trajectory_source is None and result.status == "ok":
            raise RuntimeError(
                "User Simulator finished but no canonical ACTF trajectory was found"
            )
        if source is not None:
            if source.resolve() != raw_path.resolve():
                shutil.copy2(source, raw_path)
        trajectory = None
        if trajectory_source is not None:
            if trajectory_source.resolve() != actf_path.resolve():
                shutil.copy2(trajectory_source, actf_path)
            trajectory = json.loads(actf_path.read_text(encoding="utf-8"))
            if not isinstance(trajectory, dict):
                raise ValueError("canonical ACTF trajectory must be a JSON object")
            if result.status == "ok":
                write_sft(sft_path, trajectory)
        record_training_audits(result.metadata, trajectory, simulator_dir / "wire_trace.jsonl")
        rollout_dir = simulator_dir / "rollout"
        if rollout_dir.exists():
            shutil.rmtree(rollout_dir)
        if not keep_aux_traces:
            (simulator_dir / "cache_trace.jsonl").unlink(missing_ok=True)
            (simulator_dir / "wire_trace.jsonl").unlink(missing_ok=True)
        participant = {
            "schema_version": PARTICIPANT_SCHEMA,
            "role": "user_simulator",
            "harness": harness,
            "model": model,
            "status": result.status,
            "error": result.error,
            "usage": result.usage,
            "trajectory": {
                "raw": "trace.jsonl" if raw_path.is_file() else None,
                "raw_schema": result.metadata.get("raw_trace_schema"),
                "actf": "trace_ACTF.json" if actf_path.is_file() else None,
                "actf_schema": "ACTF_v1.0" if actf_path.is_file() else None,
                "sft": "trace_SFT.json" if sft_path.is_file() else None,
            },
            "runtime": {
                key: value
                for key, value in result.metadata.items()
                if key not in {"output_collection", "provenance"}
            },
            "provenance": result.metadata.get("provenance", {}),
        }
        atomic_write_json(simulator_dir / "info.json", participant)
        return participant

    def run(self, request: RunRequest, output_dir: Path, attempt: int) -> HarnessResult:
        spec = request.task.user_simulator
        if spec is None:
            return self.agent_harness.run(request, output_dir, attempt)

        started = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        deadline = started + request.timeout_seconds
        simulator_dir = output_dir / "user_simulator"
        if simulator_dir.exists():
            shutil.rmtree(simulator_dir)
        simulator_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "conversation.json").unlink(missing_ok=True)

        simulator_request = self._simulator_request(request, spec)
        agent_session = self.agent_harness.create_session(
            request, output_dir, attempt, collect_outputs=True
        )
        simulator_session = self.simulator_harness.create_session(
            simulator_request,
            simulator_dir,
            attempt,
            collect_outputs=False,
        )
        transcript: list[dict[str, str]] = []
        rounds: list[dict[str, Any]] = []
        status = "error"
        error: str | None = None
        termination_reason = "error"
        termination_detail: str | None = None
        pending_instruction: str | None = None

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError(f"task exceeded {request.timeout_seconds}s")
            return value

        try:
            agent_session.start()
            simulator_session.start()
            while True:
                simulator_prompt = build_user_simulator_prompt(spec, transcript)
                simulator_reply = simulator_session.send(
                    simulator_prompt, remaining()
                ).content
                decision = parse_user_simulator_decision(simulator_reply)
                if decision.action == "finish":
                    if not rounds:
                        raise RuntimeError(
                            "User Simulator finished before issuing an instruction"
                        )
                    termination_reason = "simulator_finished"
                    termination_detail = decision.reason
                    status = "ok"
                    break
                assert decision.content is not None
                if len(rounds) >= spec.max_turns:
                    termination_reason = "max_turns"
                    termination_detail = (
                        "User Simulator proposed another instruction after the "
                        "configured turn limit"
                    )
                    pending_instruction = decision.content
                    status = "ok"
                    break
                transcript.append({"role": "user", "content": decision.content})
                agent_reply = agent_session.send(
                    decision.content, remaining()
                ).content
                transcript.append({"role": "assistant", "content": agent_reply})
                rounds.append(
                    {
                        "round": len(rounds) + 1,
                        "user": decision.content,
                        "assistant": agent_reply,
                    }
                )
        except (TimeoutError, subprocess.TimeoutExpired):
            status = "timeout"
            termination_reason = "timeout"
            error = f"task exceeded {request.timeout_seconds}s"
            termination_detail = error
        except Exception as exc:
            status = "error"
            termination_reason = "error"
            error = str(exc)
            termination_detail = error

        agent_result = agent_session.finish(status, error)
        simulator_result = simulator_session.finish(status, error)
        if status == "ok" and agent_result.status != "ok":
            status, error = agent_result.status, agent_result.error
            termination_reason = "agent_error"
            termination_detail = error
        if status == "ok" and simulator_result.status != "ok":
            status, error = simulator_result.status, simulator_result.error
            termination_reason = "simulator_error"
            termination_detail = error

        try:
            simulator_info = self._archive_simulator(
                simulator_dir,
                simulator_result,
                simulator_request.model,
                self.simulator_harness.name,
                keep_aux_traces=self.keep_aux_traces,
            )
        except Exception as exc:
            rollout_dir = simulator_dir / "rollout"
            if rollout_dir.exists():
                shutil.rmtree(rollout_dir)
            if not self.keep_aux_traces:
                (simulator_dir / "cache_trace.jsonl").unlink(missing_ok=True)
                (simulator_dir / "wire_trace.jsonl").unlink(missing_ok=True)
            raw_path = simulator_dir / "trace.jsonl"
            actf_path = simulator_dir / "trace_ACTF.json"
            sft_path = simulator_dir / "trace_SFT.json"
            sft_path.unlink(missing_ok=True)
            simulator_info = {
                "schema_version": PARTICIPANT_SCHEMA,
                "role": "user_simulator",
                "harness": self.simulator_harness.name,
                "model": simulator_request.model,
                "status": "error",
                "error": str(exc),
                "usage": simulator_result.usage,
                "trajectory": {
                    "raw": "trace.jsonl" if raw_path.is_file() else None,
                    "raw_schema": simulator_result.metadata.get(
                        "raw_trace_schema"
                    ),
                    "actf": "trace_ACTF.json" if actf_path.is_file() else None,
                    "actf_schema": "ACTF_v1.0" if actf_path.is_file() else None,
                    "sft": "trace_SFT.json" if sft_path.is_file() else None,
                },
                "runtime": {
                    key: value
                    for key, value in simulator_result.metadata.items()
                    if key not in {"output_collection", "provenance"}
                },
                "provenance": simulator_result.metadata.get("provenance", {}),
            }
            atomic_write_json(simulator_dir / "info.json", simulator_info)
            if status == "ok":
                status = "error"
                error = f"User Simulator trace conversion failed: {exc}"
                termination_reason = "simulator_trace_error"
                termination_detail = error

        conversation = {
            "schema_version": CONVERSATION_SCHEMA,
            "instruction": spec.instruction,
            "participants": {
                "user_simulator": {
                    "harness": self.simulator_harness.name,
                    "model": simulator_request.model,
                },
                "task_agent": {
                    "harness": self.agent_harness.name,
                    "model": request.model,
                },
            },
            "rounds": rounds,
            "status": status,
            "termination": {
                "reason": termination_reason,
                "detail": termination_detail,
                "pending_instruction": pending_instruction,
            },
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        atomic_write_json(output_dir / "conversation.json", conversation)

        usage = _combined_usage(agent_result.usage, simulator_result.usage)
        elapsed = time.monotonic() - started
        usage["elapsed_seconds"] = round(elapsed, 3)
        metadata = dict(agent_result.metadata)
        metadata["conversation"] = {
            "schema_version": CONVERSATION_SCHEMA,
            "path": "conversation.json",
            "rounds": len(rounds),
            "max_turns": spec.max_turns,
            "termination_reason": termination_reason,
            "participants": {
                "task_agent": {
                    "harness": self.agent_harness.name,
                    "model": request.model,
                    "usage": agent_result.usage,
                    "trajectory": {
                        "raw": "trace.jsonl" if agent_result.trace_path else None,
                        "raw_schema": agent_result.metadata.get("raw_trace_schema"),
                        "actf": (
                            "trace_ACTF.json"
                            if agent_result.trajectory_path
                            else None
                        ),
                        "sft": (
                            "trace_SFT.json"
                            if agent_result.trajectory_path
                            else None
                        ),
                    },
                },
                "user_simulator": {
                    "harness": self.simulator_harness.name,
                    "model": simulator_request.model,
                    "usage": simulator_result.usage,
                    "trajectory": simulator_info.get("trajectory"),
                    "path": "user_simulator",
                },
            },
        }
        return HarnessResult(
            status=status,
            elapsed_seconds=elapsed,
            error=error,
            usage=usage,
            trace_path=agent_result.trace_path,
            trajectory_path=agent_result.trajectory_path,
            metadata=metadata,
        )
