"""WildClawBench adapter.

WildClawBench ships each task as one markdown file: flat YAML frontmatter, a
``## Prompt`` for the agent, and a ``## Automated Checks`` block holding the
Python ``grade()`` that scores the finished container. This module translates
those files into ``roller.task/v1`` rows, moving each section to whichever part
of Roller's task schema already does that job — prompt, env, setup commands,
workspace mount — and parking the grader in the row's ``bench`` block.

The frontmatter is deliberately parsed by hand rather than with PyYAML: it is
flat ``key: value`` throughout, and Roller has no runtime dependencies.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from roller.benches.base import BenchAdapter, BenchBinding, BenchFormatError, GradeSpec

logger = logging.getLogger(__name__)

TASK_SCHEMA = "roller.task/v1"
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)
SECTION_RE = re.compile(r"^##\s+(.+)$")
CODE_FENCE_OPEN_RE = re.compile(r"^```[^\n]*\n?")
CODE_FENCE_CLOSE_RE = re.compile(r"\n?```$")

# WildClaw graders resolve their files against the workspace mount. Roller's
# OpenClaw harness already defaults to this same path, and the transcript
# location is left unset so the live session reports it — it moves with the
# OpenClaw version, which a hardcoded path here would get wrong.
WORKSPACE_CONTAINER_PATH = "/tmp_workspace"
DEFAULT_TIMEOUT_SECONDS = 300
# ``## Skills`` names resolve to directories under this folder of the checkout.
SKILLS_DIR_NAME = "skills"
GRADE_TIMEOUT_SECONDS = 120

# Subdirectories of a task's declared ``## Workspace Path``. Only ``exec`` is the
# agent's workspace; ``tmp`` holds warmup fixtures and ``gt`` the expected
# output, which the agent must never see.
SEED_DIR_NAME = "exec"
FIXTURES_DIR_NAME = "tmp"
ANSWER_KEY_DIR_NAME = "gt"

# Upstream's runner prepends this to every task's prompt rather than shipping it
# in the task file (``eval/run_batch.py``: built as ``system_prompt``, then
# concatenated into the user prompt — it is not sent as a system message). The
# task's own timeout is interpolated, so the agent is told the budget it has.
# Reproducing it here is what makes our prompts byte-identical to upstream's;
# without it every one of the 60 tasks is asking a different question.
PROMPT_PREAMBLE = (
    "You are an expert in a restricted, non-interactive environment. Solve the "
    "task efficiently before the timeout ({timeout_seconds}s). Run all processes "
    "in the foreground without user input or background services. Provide a "
    "complete, functional solution in a single pass with no placeholders. \n"
)


def apply_prompt_preamble(prompt: str, timeout_seconds: int) -> str:
    return PROMPT_PREAMBLE.format(timeout_seconds=timeout_seconds) + prompt


# Upstream does not let ``web_search`` reach the real Brave API: it points the
# hostname at a local Brave-protocol shim (``tools/serper_brave_shim.py``) with
# ``--add-host`` plus a mounted CA certificate. Without that wiring the hostname
# resolves through the host's DNS, the connection fails, and every search comes
# back ``{"status": "error", "error": "fetch failed"}`` — a silent capability
# loss, because the agent keeps working and the run still produces scores.
# Roller has no ``--add-host`` or extra-mount option, so the same wiring is
# expressed as setup commands, which run as root before the agent starts.
SHIM_CERT_CONTAINER_PATH = "/usr/local/share/ca-certificates/wildclaw-search-shim.crt"
SEARCH_SHIM_HOSTNAME_DEFAULT = "api.search.brave.com"


@dataclass(frozen=True)
class SearchShim:
    """Where the Brave-protocol search shim listens, as seen from the container."""

    hostname: str
    host_ip: str
    certificate: str | None = None

    def setup_commands(self) -> list[str]:
        entry = f"{self.host_ip} {self.hostname}"
        commands = [
            # Idempotent: setup may run again on ``--resume`` against a fresh
            # container, and a duplicate line would still work but reads as a bug.
            f"grep -qxF {shlex.quote(entry)} /etc/hosts || "
            f"echo {shlex.quote(entry)} >> /etc/hosts"
        ]
        if self.certificate is None:
            return commands
        # The certificate is written inline rather than staged as a workspace
        # file: it is public, and anything copied into the workspace also lands
        # in the agent's view of the task.
        commands.append(
            f"cat > {SHIM_CERT_CONTAINER_PATH} <<'ROLLER_WILDCLAW_SHIM_CERT'\n"
            f"{self.certificate.strip()}\n"
            "ROLLER_WILDCLAW_SHIM_CERT\n"
            "update-ca-certificates >/dev/null 2>&1 || true; "
            # requests/httpx read certifi's bundle, not the system store.
            'for bundle in $(python3 -c "import certifi;print(certifi.where())" '
            "2>/dev/null); do "
            f'grep -qF "$(sed -n 2p {SHIM_CERT_CONTAINER_PATH})" "$bundle" 2>/dev/null || '
            f'cat {SHIM_CERT_CONTAINER_PATH} >> "$bundle"; done'
        )
        return commands


def search_shim_from_env(
    root: Path, environ: dict[str, str] | None = None
) -> SearchShim | None:
    """Read the shim's location from upstream's own environment variables.

    Same names and same defaults as ``src/utils/docker_utils.py``, including the
    relative ``SEARCH_SHIM_CERT`` path being resolved against the checkout.
    Returns ``None`` when the shim is not configured, which is upstream's
    behaviour too — the tasks still run, they just cannot search.
    """

    env = os.environ if environ is None else environ
    host_ip = (env.get("SEARCH_SHIM_HOST_IP") or "").strip()
    if not host_ip:
        return None
    hostname = (env.get("SEARCH_SHIM_HOSTNAME") or "").strip() or SEARCH_SHIM_HOSTNAME_DEFAULT
    raw_cert = (env.get("SEARCH_SHIM_CERT") or "").strip()
    certificate: str | None = None
    if raw_cert:
        cert_path = Path(raw_cert)
        if not cert_path.is_absolute():
            cert_path = root / cert_path
        if cert_path.is_file():
            certificate = cert_path.read_text(encoding="utf-8")
        else:
            # A self-signed shim without its certificate fails TLS on every
            # search, which looks exactly like "the model did not search".
            logger.warning(
                "SEARCH_SHIM_CERT is set but %s is not a file; searches will fail "
                "TLS verification",
                cert_path,
            )
    return SearchShim(hostname=hostname, host_ip=host_ip, certificate=certificate)


def strip_code_fence(raw: str) -> str:
    text = CODE_FENCE_OPEN_RE.sub("", raw.strip())
    return CODE_FENCE_CLOSE_RE.sub("", text).strip()


def parse_frontmatter(block: str, label: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition(":")
        if not separator:
            raise BenchFormatError(f"{label}: frontmatter line is not key: value — {line!r}")
        metadata[key.strip()] = value.strip().strip("'\"")
    return metadata


def split_sections(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in body.split("\n"):
        header = SECTION_RE.match(line)
        if header:
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current = header.group(1).strip()
            lines = []
        else:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()
    return sections


def parse_task_markdown(text: str, label: str) -> dict[str, Any]:
    """Parse one WildClaw task file's text into its raw fields.

    ``label`` only names the source in error messages, so this works on a string
    and stays testable without touching the filesystem.
    """

    stem = Path(label).stem
    match = FRONTMATTER_RE.match(text)
    if match:
        metadata = parse_frontmatter(match.group(1), label)
        body = match.group(2)
    else:
        # A few upstream tasks carry no frontmatter; the filename still encodes
        # the id and category, so fall back rather than reject the task.
        metadata = {}
        body = text
    sections = split_sections(body)

    prompt = sections.get("Prompt", "").strip()
    if not prompt:
        raise BenchFormatError(f"{label}: missing ## Prompt")
    workspace = strip_code_fence(sections.get("Workspace Path", ""))

    raw_timeout = metadata.get("timeout_seconds", str(DEFAULT_TIMEOUT_SECONDS))
    try:
        timeout_seconds = int(raw_timeout)
    except ValueError:
        raise BenchFormatError(
            f"{label}: timeout_seconds must be an integer, got {raw_timeout!r}"
        ) from None
    if timeout_seconds <= 0:
        raise BenchFormatError(f"{label}: timeout_seconds must be positive")

    return {
        "id": metadata.get("id") or stem,
        "name": metadata.get("name", ""),
        "category": metadata.get("category") or _category_from_stem(stem),
        "modality": metadata.get("modality", ""),
        "timeout_seconds": timeout_seconds,
        "prompt": prompt,
        "workspace": workspace,
        "grader_code": strip_code_fence(sections.get("Automated Checks", "")),
        "env_names": _lines(strip_code_fence(sections.get("Env", ""))),
        "skills": _lines(strip_code_fence(sections.get("Skills", ""))),
        "warmup": _lines(strip_code_fence(sections.get("Warmup", ""))),
    }


def _category_from_stem(stem: str) -> str:
    """Recover ``01_Productivity_Flow`` from ``01_Productivity_Flow_task_3_x``."""

    head, separator, _ = stem.partition("_task_")
    return head if separator else ""


def _lines(block: str) -> list[str]:
    return [
        line.strip()
        for line in block.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


class WildClawBenchAdapter(BenchAdapter):
    """Load a WildClawBench checkout as Roller task rows.

    Categories are reported from what the checkout actually holds rather than
    from upstream's list of six: a partial or trimmed checkout has fewer, and
    naming a category that is not there would produce a silently empty run.
    """

    name = "wildclawbench"
    description = "WildClawBench — 60 real-world OpenClaw agent tasks across 6 categories"

    def __init__(self, search_shim: SearchShim | None = None) -> None:
        # ``None`` means "read upstream's SEARCH_SHIM_* variables at export time";
        # an explicit value is for tests and for callers that already know where
        # their shim listens.
        self._search_shim = search_shim

    def load(
        self,
        root: Path,
        *,
        categories: Sequence[str] | None = None,
        task_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        tasks_root = self._tasks_root(root)
        shim = self._search_shim or search_shim_from_env(root)
        if shim is None:
            logger.info(
                "SEARCH_SHIM_HOST_IP is unset: web_search will not resolve inside the "
                "container and every search will return 'fetch failed'"
            )
        else:
            logger.info(
                "search shim: %s -> %s (certificate %s)",
                shim.hostname,
                shim.host_ip,
                "trusted" if shim.certificate else "absent",
            )
        wanted_categories = {c for c in categories or ()}
        wanted_ids = {t for t in task_ids or ()}
        rows: list[dict[str, Any]] = []
        for path in sorted(tasks_root.rglob("*.md")):
            if path.name.startswith("task0_template") or path.name == "README.md":
                continue
            parsed = parse_task_markdown(
                path.read_text(encoding="utf-8"), str(path)
            )
            if not parsed["category"]:
                parsed["category"] = path.parent.name
            if wanted_categories and parsed["category"] not in wanted_categories:
                continue
            if wanted_ids and parsed["id"] not in wanted_ids:
                continue
            parsed["source_path"] = str(path.resolve())
            rows.append(self._to_row(parsed, root, shim))
        if wanted_ids:
            missing = wanted_ids - {row["id"] for row in rows}
            if missing:
                raise BenchFormatError(
                    f"task ids not found under {tasks_root}: {', '.join(sorted(missing))}"
                )
        return rows

    @staticmethod
    def _tasks_root(root: Path) -> Path:
        if not root.is_dir():
            raise FileNotFoundError(f"WildClawBench root is not a directory: {root}")
        nested = root / "tasks"
        return nested if nested.is_dir() else root

    def _skill_context(self, parsed: dict[str, Any], root: Path) -> list[dict[str, Any]]:
        """Turn each ``## Skills`` name into a staged ``role: skill`` context entry.

        The names refer to directories under the checkout's ``skills/``. Recording
        them in bench metadata alone leaves the agent reading a prompt that tells
        it to follow a skill the container never received, which scores zero in a
        way that looks like a model failure.
        """

        entries: list[dict[str, Any]] = []
        for name in parsed["skills"]:
            source = (root / SKILLS_DIR_NAME / name).resolve()
            if not source.is_dir():
                logger.warning(
                    "[%s] declared skill not found: %s (task will run without it)",
                    parsed["id"],
                    source,
                )
                continue
            entries.append(
                {
                    "role": "skill",
                    "source": str(source),
                    "target": f"skills/{name}",
                }
            )
        return entries

    def _to_row(
        self, parsed: dict[str, Any], root: Path, shim: SearchShim | None = None
    ) -> dict[str, Any]:
        declared = parsed["workspace"]
        declared_root = Path(declared) if declared else None
        if declared_root is not None and not declared_root.is_absolute():
            declared_root = (root / declared_root).resolve()
        seed, fixtures, answer_key = self._workspace_parts(parsed["id"], declared_root)
        grade = (
            GradeSpec(
                code=parsed["grader_code"],
                entrypoint="grade",
                env_names=tuple(parsed["env_names"]),
                timeout_seconds=GRADE_TIMEOUT_SECONDS,
                needs_transcript=True,
                workspace_path=WORKSPACE_CONTAINER_PATH,
                score_key="overall_score",
                stage=(
                    ((str(answer_key), ANSWER_KEY_DIR_NAME),)
                    if answer_key is not None
                    else ()
                ),
            )
            if parsed["grader_code"]
            else None
        )
        binding = BenchBinding(
            name=self.name,
            grade=grade,
            category=parsed["category"],
            metadata={
                key: value
                for key, value in (
                    ("name", parsed["name"]),
                    ("modality", parsed["modality"]),
                    ("source_path", parsed.get("source_path", "")),
                    ("skills", parsed["skills"]),
                    ("workspace", parsed["workspace"]),
                )
                if value
            },
        )
        row: dict[str, Any] = {
            "schema_version": TASK_SCHEMA,
            "id": parsed["id"],
            "harness": "openclaw",
            "prompt": apply_prompt_preamble(
                parsed["prompt"], parsed["timeout_seconds"]
            ),
            "timeout_seconds": parsed["timeout_seconds"],
            "bench": binding.as_dict(),
        }
        if seed is not None:
            # ``workspace`` (not ``file_root``) is the key that actually mounts
            # the directory: Roller copies its contents into the container's
            # workspace, which is what upstream does with ``<declared>/exec``.
            row["workspace"] = str(seed)
        context = self._skill_context(parsed, root)
        if fixtures is not None:
            # Upstream copies ``<declared>/tmp`` to ``<workspace>/tmp`` — warmup
            # scripts read their fixtures from there.
            context.append(
                {
                    "role": "fixture",
                    "source": str(fixtures),
                    "target": FIXTURES_DIR_NAME,
                    "capture": "none",
                }
            )
        if context:
            row["files"] = {"context": context}
        # The shim has to be wired up before the agent runs, so it goes ahead of
        # the task's own warmup commands.
        setup = (shim.setup_commands() if shim is not None else []) + list(parsed["warmup"])
        if parsed["env_names"] or setup:
            runtime: dict[str, Any] = {}
            if parsed["env_names"]:
                runtime["env"] = list(parsed["env_names"])
            if setup:
                runtime["setup"] = setup
            row["runtime"] = runtime
        return row

    @staticmethod
    def _workspace_parts(
        task_id: str, declared: Path | None
    ) -> tuple[Path | None, Path | None, Path | None]:
        """Split a declared workspace into (seed, fixtures, answer key).

        A WildClaw workspace directory is not itself the agent's workspace: it
        holds ``exec/`` (the seed files the agent starts from), an optional
        ``tmp/`` (warmup fixtures) and an optional ``gt/`` (the expected output).
        Upstream mounts only ``exec/`` and copies ``gt/`` in at grading time, so
        splitting them here is what keeps the answer key out of the agent's
        reach — mounting the declared directory wholesale hands it the answers.
        """

        if declared is None:
            return None, None, None
        if not declared.is_dir():
            # Seed files declared but absent: the agent would start in an empty
            # workspace and score zero for reasons that look like model failure.
            # Say so at export time instead.
            logger.warning(
                "[%s] declared workspace not found: %s (task will run without seed files)",
                task_id,
                declared,
            )
            return None, None, None
        seed = declared / SEED_DIR_NAME
        if not seed.is_dir():
            # Every upstream task ships ``exec/``, but a trimmed checkout may
            # not. Falling back to the declared directory keeps such a task
            # runnable, so warn loudly about what that exposes.
            logger.warning(
                "[%s] workspace has no %s/ directory; mounting %s wholesale, which "
                "may expose grading data to the agent",
                task_id,
                SEED_DIR_NAME,
                declared,
            )
            seed = declared
        fixtures = declared / FIXTURES_DIR_NAME
        answer_key = declared / ANSWER_KEY_DIR_NAME
        if seed == declared:
            # Nothing was split off, so both are already inside the mount and
            # there is no separate staging to do.
            return seed, None, None
        return (
            seed,
            fixtures if fixtures.is_dir() else None,
            answer_key if answer_key.is_dir() else None,
        )
