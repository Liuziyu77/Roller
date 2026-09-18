"""SkillsBench adapter with task environments and skill ablation variants."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import yaml

from roller.benches.base import (
    BenchAdapter,
    BenchBinding,
    BenchFormatError,
    GradeSpec,
    validate_timeout_multiplier,
)
from roller.benches.report import REPORT_SCHEMA, sample_score

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)
WORKDIR_RE = re.compile(r"^\s*WORKDIR\s+(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
ENV_RE = re.compile(r"^\s*ENV\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)\s*$", re.MULTILINE)
SKILL_COPY_RE = re.compile(r"^\s*COPY\s+(?:_deps/)?skills(?:/)?\s+.*$", re.MULTILINE)
HEREDOC_RE = re.compile(r"<<-?['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?\s*$")
PYTHON_STDIN_RE = re.compile(r"\b(python3?\s+-)\s+<<-?['\"]?[A-Za-z_][A-Za-z0-9_]*['\"]?\s*$")
CAT_REDIRECT_RE = re.compile(r"\bcat\s+>\s+([^\s]+)\s+<<-?['\"]?[A-Za-z_][A-Za-z0-9_]*['\"]?\s*$")
FROM_RE = re.compile(r"^(\s*FROM\s+(?:--platform=\S+\s+)?)(\S+)(.*)$", re.IGNORECASE)
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
RESOURCE_KEYS = {
    "network_mode",
    "build_timeout_sec",
    "os",
    "cpus",
    "memory_mb",
    "storage_mb",
    "gpus",
}

_VERIFIER_RUNNER = r'''
import os
from pathlib import Path
import subprocess
import tempfile
import time


_RETRYABLE_INFRASTRUCTURE_ERRORS = {
    "verifier dependency download failed",
    "verifier DNS failure",
}


def _infrastructure_error(output):
    lowered = output.lower()
    tests_completed = (
        "short test summary info" in lowered
        or (
            "test session starts" in lowered
            and any(marker in lowered for marker in (" passed in ", " failed in ", " error in "))
        )
    )
    markers = (
        ("uvx: command not found", "uvx is unavailable", False),
        ("uv: command not found", "uv is unavailable", False),
        ("failed to download", "verifier dependency download failed", True),
        ("temporary failure in name resolution", "verifier DNS failure", True),
        ("could not resolve host", "verifier DNS failure", True),
        ("executable doesn't exist at", "Playwright browser is unavailable", False),
        ("please run the following command to download new browsers", "Playwright browser is unavailable", False),
        ("browser executable not found", "Playwright browser is unavailable", False),
        ("err_access_denied", "local verifier request was intercepted by a proxy", False),
    )
    for marker, message, ignore_after_tests in markers:
        if marker in lowered:
            if ignore_after_tests and tests_completed:
                continue
            return message
    return None


def roller_grade(workspace_path, task_workspace, verifier_env):
    environment = dict(os.environ)
    environment.update({str(key): str(value) for key, value in verifier_env.items()})
    reward_path = Path("/logs/verifier/reward.txt")
    reward_path.parent.mkdir(parents=True, exist_ok=True)
    # Official harnesses provide these mount points. Roller stages the verifier
    # into the live task container, so create their empty equivalents here.
    Path("/tests").mkdir(parents=True, exist_ok=True)
    # v1.1 verifier scripts repeatedly bootstrap uv from the network and then
    # source this file. The task overlay already ships pinned uv/uvx binaries;
    # this fallback lets verification proceed when the redundant download is
    # interrupted by a cluster proxy.
    local_bin = Path.home() / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    env_path = local_bin / "env"
    if not env_path.exists():
        env_path.write_text('export PATH="$HOME/.local/bin:$PATH"\n', encoding="utf-8")
    result = None
    output = ""
    verifier_attempts = 0
    for verifier_attempts in range(1, 4):
        reward_path.unlink(missing_ok=True)
        # Some upstream verifiers leave a background service alive after their
        # shell exits.  A PIPE would remain open in that descendant and make
        # subprocess.run() wait until the outer timeout despite completed tests.
        # A regular file lets us observe the verifier process itself, while the
        # disposable task container still cleans up any leftover service.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as log:
            result = subprocess.run(
                ["/bin/bash", "/verifier/test.sh"],
                cwd=task_workspace,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=__VERIFIER_TIMEOUT__,
            )
            log.flush()
            output = Path(log.name).read_text(encoding="utf-8", errors="replace")
        infrastructure_error = _infrastructure_error(output)
        if (
            infrastructure_error not in _RETRYABLE_INFRASTRUCTURE_ERRORS
            or verifier_attempts == 3
        ):
            break
        time.sleep(5 * verifier_attempts)
    assert result is not None
    if not reward_path.is_file():
        raise RuntimeError(
            "SkillsBench verifier produced no /logs/verifier/reward.txt: "
            + output[-2000:]
        )
    try:
        reward = float(reward_path.read_text(encoding="utf-8").strip())
    except ValueError as exc:
        raise RuntimeError("SkillsBench verifier reward is not numeric") from exc
    payload = {
        "overall_score": max(0.0, min(1.0, reward)),
        "passed": reward > 0,
        "score_eligible": True,
        "verifier_exit_code": result.returncode,
        "verifier_attempts": verifier_attempts,
        "verifier_output": output[-8000:],
    }
    infrastructure_error = _infrastructure_error(output)
    if infrastructure_error:
        payload["score_eligible"] = False
        payload["error"] = infrastructure_error
    return payload
'''


_INFRASTRUCTURE_OUTPUT_MARKERS = (
    "uvx: command not found",
    "uv: command not found",
    "failed to download",
    "temporary failure in name resolution",
    "could not resolve host",
    "executable doesn't exist at",
    "please run the following command to download new browsers",
    "browser executable not found",
    "err_access_denied",
)


def _score_error(row: dict[str, Any], scores: dict[str, Any] | None) -> str | None:
    error = row.get("bench_error")
    if isinstance(error, str) and error:
        return error
    if not isinstance(scores, dict):
        return None
    error = scores.get("error")
    if isinstance(error, str) and error:
        return error
    output = scores.get("verifier_output")
    if isinstance(output, str):
        lowered = output.lower()
        tests_completed = (
            "short test summary info" in lowered
            or (
                "test session starts" in lowered
                and any(
                    marker in lowered
                    for marker in (" passed in ", " failed in ", " error in ")
                )
            )
        )
        for marker in _INFRASTRUCTURE_OUTPUT_MARKERS:
            if marker in lowered:
                if marker in {
                    "failed to download",
                    "temporary failure in name resolution",
                    "could not resolve host",
                } and tests_completed:
                    continue
                return f"verifier infrastructure failure: {marker}"
    return None


def _task_metadata(path: Path) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    if match is None:
        raise BenchFormatError(f"{path}: missing YAML frontmatter")
    try:
        metadata = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise BenchFormatError(f"{path}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(metadata, dict):
        raise BenchFormatError(f"{path}: frontmatter must be an object")
    prompt = match.group(2).strip()
    if not prompt:
        raise BenchFormatError(f"{path}: task prompt is empty")
    return metadata, prompt


def _portable_heredocs(value: str) -> str:
    """Rewrite Dockerfile heredocs for engines whose parser predates BuildKit.

    SkillsBench currently uses Python-stdin, file-writing, and direct RUN
    heredocs.  Their shell-equivalent forms work in both Docker and Podman 4.x.
    """

    lines = value.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        marker = HEREDOC_RE.search(line)
        if marker is None:
            output.append(line)
            index += 1
            continue
        delimiter = marker.group(1)
        end = index + 1
        while end < len(lines) and lines[end].strip() != delimiter:
            end += 1
        if end == len(lines):
            raise BenchFormatError(f"unterminated Dockerfile heredoc {delimiter!r}")
        body = "\n".join(lines[index + 1 : end]) + "\n"
        if ".from_pretrained(" in body:
            original = base64.b64encode(body.encode("utf-8")).decode("ascii")
            backup = (
                "\n            import seisbench\n"
                "            seisbench.use_backup_repository()"
                if "seisbench.models" in body
                else ""
            )
            body = f'''import base64
import time

source = base64.b64decode({original!r}).decode("utf-8")
last_error = None
for attempt in range(6):
    try:
        exec(compile(source, "<skillsbench-preload>", "exec"), globals())
        break
    except Exception as exc:
        last_error = exc
        if attempt == 0:{backup or chr(10) + "            pass"}
        if attempt == 5:
            raise
        time.sleep(20 * (attempt + 1))
else:
    raise RuntimeError("SkillsBench preload failed") from last_error
'''
        encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
        # SeisBench 0.10 imports pkg_resources, which was removed from recent
        # setuptools releases selected by an otherwise-unpinned upstream pip
        # install. Pin the last compatible series immediately before its cache
        # warm-up script so current clean builds remain reproducible.
        compatibility = (
            "pip install --no-cache-dir 'setuptools<81' && "
            if "import seisbench" in body
            else ""
        )
        python_match = PYTHON_STDIN_RE.search(line)
        cat_match = CAT_REDIRECT_RE.search(line)
        if python_match is not None:
            replacement = (
                f"{compatibility}printf %s {encoded} | base64 -d | "
                f"{python_match.group(1)}"
            )
            output.append(line[: python_match.start()] + replacement)
        elif cat_match is not None:
            replacement = f"printf %s {encoded} | base64 -d > {cat_match.group(1)}"
            output.append(line[: cat_match.start()] + replacement)
        elif line.strip().upper().startswith("RUN <<"):
            shell = "/bin/bash" if "pipefail" in body else "/bin/sh"
            output.append(f"RUN printf %s {encoded} | base64 -d | {shell}")
        else:
            raise BenchFormatError(
                f"unsupported Dockerfile heredoc syntax: {line.strip()}"
            )
        index = end + 1
    return "\n".join(output)


def _qualified_from_images(value: str) -> str:
    """Apply Docker Hub's implicit namespace rule for strict Podman hosts."""

    output: list[str] = []
    for line in value.splitlines():
        match = FROM_RE.match(line)
        if match is None:
            output.append(line)
            continue
        image = match.group(2)
        first_component = image.split("/", 1)[0]
        if (
            "/" in image
            and not image.startswith("$")
            and "." not in first_component
            and ":" not in first_component
            and first_component != "localhost"
        ):
            image = "docker.io/" + image
        output.append(match.group(1) + image + match.group(3))
    return "\n".join(output)


def _portable_tar_extraction(value: str) -> str:
    """Prevent archive ownership metadata from breaking rootless builds."""

    output: list[str] = []
    for line in value.splitlines():
        extraction = re.search(
            r"(?:^\s*|[;&|]\s*|\bRUN\s+)tar\s+"
            r"(?:--[\w-]+(?:=[^\s]+)?\s+)*-[A-Za-z]*x[A-Za-z]*\b",
            line,
            re.IGNORECASE,
        )
        if extraction is not None:
            if "--no-same-owner" not in line:
                line = re.sub(
                    r"\btar\s+",
                    "tar --no-same-owner --no-same-permissions ",
                    line,
                    count=1,
                    flags=re.IGNORECASE,
                )
        output.append(line)
    return "\n".join(output)


def _sanitized_dockerfile(path: Path) -> str:
    value = _portable_heredocs(path.read_text(encoding="utf-8"))
    value = _qualified_from_images(value)
    value = _portable_tar_extraction(value)
    # v1.1 predates the upstream PyCBC compatibility pin. Retain the release
    # task while applying the same dependency-only fix now present on main.
    if "pycbc==2.8.5" in value and "astropy==7.2.1" not in value:
        value = value.replace(
            '    "setuptools<81" \\\n',
            '    "setuptools<81" \\\n    "astropy==7.2.1" \\\n',
            1,
        )
    # Coursier's interactive setup installs a newer copy of itself from GitHub
    # before installing Scala. That extra download is unnecessary and is less
    # reliable behind evaluation-cluster proxies; the pinned launcher already
    # present in the image can install the requested Scala binaries directly.
    if "coursier/releases/download/" in value:
        value = value.replace("curl -fL ", "curl -fL --retry 5 --retry-all-errors --retry-delay 5 ")
        value = value.replace("./cs setup --yes && ./cs install ", "./cs install ")
    # A no-skill run must not inherit task skills accidentally. Runtime staging
    # injects them only for the with-skill condition.
    value = SKILL_COPY_RE.sub("# Roller: task skills are injected at runtime", value)
    return value.rstrip() + "\n"


def _workspace_from_dockerfile(source: str) -> str:
    environment = {name: value.strip('"\'') for name, value in ENV_RE.findall(source)}
    matches = WORKDIR_RE.findall(source)
    value = matches[-1].strip().strip('"\'') if matches else "/root"
    value = VAR_RE.sub(lambda match: environment.get(match.group(1), match.group(0)), value)
    return value if value.startswith("/") else "/root"


def _environment_digest(root: Path, dockerfile: str) -> str:
    digest = hashlib.sha256()
    # Bump this salt whenever the harness overlay contract changes so a stale
    # locally tagged task image cannot silently bypass the new layer.
    digest.update(b"roller-skillsbench-opencode-v2\0")
    digest.update(dockerfile.encode("utf-8"))
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "skills" in relative.parts or not path.is_file():
            continue
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def _image_component(value: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip("-.") or "task"


def _task_set(root: Path) -> list[dict[str, Any]]:
    path = root / "integrations" / "agentbeats" / "task_sets" / "skillsbench-v1.1.json"
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BenchFormatError(f"invalid SkillsBench task set {path}: {exc}") from exc
        tasks = payload.get("tasks") if isinstance(payload, dict) else None
        if isinstance(tasks, list) and all(isinstance(item, dict) for item in tasks):
            return tasks
    return [{"task_id": path.name} for path in sorted((root / "tasks").iterdir()) if path.is_dir()]


class SkillsBenchAdapter(BenchAdapter):
    name = "skillsbench"
    description = "SkillsBench — task-specific environments, with-skill/no-skill ablation"
    variants = ("with-skill", "no-skill")
    default_variant = "with-skill"

    @staticmethod
    def _root(root: Path) -> Path:
        root = root.expanduser().resolve()
        if not (root / "tasks").is_dir():
            raise FileNotFoundError(f"SkillsBench tasks/ not found under: {root}")
        return root

    def load(
        self,
        root: Path,
        *,
        categories: Sequence[str] | None = None,
        task_ids: Sequence[str] | None = None,
        variant: str | None = None,
        timeout_multiplier: float = 1.0,
    ) -> list[dict[str, Any]]:
        root = self._root(root)
        aliases = {
            "with-skills": "with-skill",
            "without-skills": "no-skill",
            "without-skill": "no-skill",
        }
        variant = aliases.get(variant or "", variant) or self.default_variant
        if variant not in self.variants:
            raise BenchFormatError(
                f"{self.name}: variant must be one of {', '.join(self.variants)}"
            )
        entries = _task_set(root)
        known = {
            item.get("task_id") for item in entries if isinstance(item.get("task_id"), str)
        }
        requested = set(task_ids or ())
        missing = requested - known
        if missing:
            raise BenchFormatError(
                f"{self.name}: unknown task id(s): {', '.join(sorted(missing))}"
            )
        category_filter = set(categories or ())
        rows: list[dict[str, Any]] = []
        for entry in entries:
            task_id = entry.get("task_id")
            if not isinstance(task_id, str):
                continue
            if requested and task_id not in requested:
                continue
            task_dir = root / "tasks" / task_id
            metadata, prompt = _task_metadata(task_dir / "task.md")
            taxonomy = metadata.get("metadata") or {}
            if not isinstance(taxonomy, dict):
                raise BenchFormatError(f"{task_id}: metadata.metadata must be an object")
            category = str(entry.get("category") or taxonomy.get("category") or "")
            if category_filter and category not in category_filter:
                continue
            rows.append(
                self._row(
                    task_dir,
                    task_id,
                    metadata,
                    prompt,
                    category,
                    variant,
                    validate_timeout_multiplier(timeout_multiplier),
                )
            )
        return rows

    def _row(
        self,
        task_dir: Path,
        task_id: str,
        metadata: dict[str, Any],
        prompt: str,
        category: str,
        variant: str,
        timeout_multiplier: float,
    ) -> dict[str, Any]:
        environment_dir = task_dir / "environment"
        dockerfile_path = environment_dir / "Dockerfile"
        verifier_dir = task_dir / "verifier"
        if not dockerfile_path.is_file() or not (verifier_dir / "test.sh").is_file():
            raise BenchFormatError(f"{task_id}: environment Dockerfile or verifier/test.sh missing")
        dockerfile = _sanitized_dockerfile(dockerfile_path)
        digest = _environment_digest(environment_dir, dockerfile)
        component = _image_component(task_id)[:80]
        short = digest[:16]
        base_image = f"roller-skillsbench-env-{component}:{short}"
        image = f"roller-skillsbench-opencode-{component}:{short}"
        agent = metadata.get("agent") or {}
        verifier = metadata.get("verifier") or {}
        # v1.1 calls this block ``environment``. Current development versions
        # renamed it to ``sandbox``; accepting both keeps the adapter useful
        # while allowing official runs to stay pinned to the v1.1 release.
        sandbox = metadata.get("sandbox") or metadata.get("environment") or {}
        if not isinstance(agent, dict) or not isinstance(verifier, dict):
            raise BenchFormatError(f"{task_id}: agent and verifier must be objects")
        if not isinstance(sandbox, dict):
            raise BenchFormatError(f"{task_id}: sandbox must be an object")
        timeout_value = agent.get("timeout_sec", 900)
        verifier_timeout_value = verifier.get("timeout_sec", 900)
        try:
            timeout = max(1, math.ceil(float(timeout_value) * timeout_multiplier))
            verifier_timeout = max(
                1, math.ceil(float(verifier_timeout_value) * timeout_multiplier)
            )
        except (TypeError, ValueError) as exc:
            raise BenchFormatError(f"{task_id}: invalid agent/verifier timeout") from exc
        build_timeout_value = sandbox.get("build_timeout_sec", 3600)
        try:
            build_timeout = max(1, math.ceil(float(build_timeout_value)))
        except (TypeError, ValueError) as exc:
            raise BenchFormatError(f"{task_id}: invalid sandbox.build_timeout_sec") from exc
        build_args = {
            str(key): value
            for key, value in sandbox.items()
            if key not in RESOURCE_KEYS and isinstance(value, (str, int, float, bool))
        }
        workdir = _workspace_from_dockerfile(dockerfile)
        skills_dir = environment_dir / "skills"
        files: dict[str, Any] = {"output_dir": ".roller_outputs"}
        setup: list[str] = []
        skill_targets = (
            "/roller/opencode-home/config/opencode/skills",
            "/root/.config/opencode/skills",
            "/root/.opencode/skill",
            "/root/.agents/skills",
            "/root/.claude/skills",
            "/root/.codex/skills",
            "/root/.gemini/skills",
            "/skills",
        )
        cleanup = "rm -rf " + " ".join(skill_targets)
        if variant == "with-skill":
            if not skills_dir.is_dir():
                raise BenchFormatError(f"{task_id}: environment/skills is missing")
            files["context"] = [
                {
                    "role": "skill",
                    "source": str(skills_dir.resolve()),
                    "target": "skills",
                    "capture": "hash",
                }
            ]
            copies = " && ".join(
                f"mkdir -p {target} && cp -a /roller/task/context/skills/. {target}/"
                for target in skill_targets
            )
            setup.append(f"{cleanup} && {copies}")
        else:
            setup.append(cleanup)
        verifier_env = verifier.get("env") or {}
        if not isinstance(verifier_env, dict):
            raise BenchFormatError(f"{task_id}: verifier.env must be an object")
        grade = GradeSpec(
            code=_VERIFIER_RUNNER.replace("__VERIFIER_TIMEOUT__", str(verifier_timeout)),
            entrypoint="roller_grade",
            kwargs={"task_workspace": workdir, "verifier_env": verifier_env},
            timeout_seconds=verifier_timeout + 30,
            needs_transcript=False,
            workspace_path="/",
            stage=((str(verifier_dir.resolve()), "verifier"),),
        )
        binding = BenchBinding(
            self.name,
            grade=grade,
            category=category,
            metadata={
                "variant": variant,
                "difficulty": (metadata.get("metadata") or {}).get("difficulty"),
                "environment_digest": digest,
                "taxonomy": dict(metadata.get("metadata") or {}),
                "resource_request": {
                    key: sandbox[key] for key in RESOURCE_KEYS if key in sandbox
                },
                "oracle_declared": "oracle" in metadata,
            },
        )
        return {
            "schema_version": "roller.task/v1",
            "id": task_id,
            "harness": "opencode",
            "prompt": prompt,
            "timeout_seconds": timeout,
            "files": files,
            "runtime": {
                "image": image,
                "image_build": {
                    "base_image": base_image,
                    "context": str(environment_dir.resolve()),
                    "dockerfile": dockerfile,
                    "harness": "opencode",
                    "args": build_args,
                    "timeout_seconds": build_timeout,
                },
                "workspace_path": workdir,
                "setup": setup,
            },
            "bench": binding.as_dict(),
        }

    def aggregate(self, samples: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Report SkillsBench's score-eligible pass rate and mean reward."""

        rows = list(samples)
        task_rows: list[dict[str, Any]] = []
        category_values: dict[str, list[tuple[float, bool]]] = {}
        rewards: list[float] = []
        passed = 0
        for row in rows:
            scores = row.get("bench_scores")
            score_key = row.get("bench_score_key") or "overall_score"
            value = sample_score(scores, score_key) if isinstance(scores, dict) else None
            error = _score_error(row, scores if isinstance(scores, dict) else None)
            declared_eligible = (
                scores.get("score_eligible", True)
                if isinstance(scores, dict)
                else False
            )
            status = row.get("status")
            eligible = bool(
                status == "ok"
                and value is not None
                and not error
                and declared_eligible is not False
            )
            is_passed = bool(
                eligible
                and (
                    scores.get("passed")
                    if isinstance(scores, dict) and isinstance(scores.get("passed"), bool)
                    else value > 0
                )
            )
            category = row.get("bench_category")
            if eligible:
                assert value is not None
                rewards.append(value)
                passed += int(is_passed)
                if isinstance(category, str) and category:
                    category_values.setdefault(category, []).append((value, is_passed))
            elif not error:
                error = str(row.get("error") or status or "not score eligible")
            task_rows.append(
                {
                    "task_id": row.get("task_id"),
                    "model": row.get("model"),
                    "category": category,
                    "status": status,
                    "score": value if eligible else None,
                    "score_eligible": eligible,
                    "passed": is_passed,
                    "error": error,
                }
            )
        eligible_count = len(rewards)
        mean_reward = sum(rewards) / eligible_count if eligible_count else None
        pass_rate = passed / eligible_count if eligible_count else None
        return {
            "schema_version": REPORT_SCHEMA,
            "metric": "pass_rate",
            "task_count": len(rows),
            "scored_count": eligible_count,
            "score_eligible_count": eligible_count,
            "unscored_count": len(rows) - eligible_count,
            "infra_failed_count": len(rows) - eligible_count,
            "passed_count": passed,
            "pass_rate": round(pass_rate, 6) if pass_rate is not None else None,
            "mean_reward": round(mean_reward, 6) if mean_reward is not None else None,
            "average": round(mean_reward, 6) if mean_reward is not None else None,
            "scored_average": round(mean_reward, 6) if mean_reward is not None else None,
            "categories": {
                name: {
                    "task_count": len(values),
                    "pass_rate": round(
                        sum(int(item[1]) for item in values) / len(values), 6
                    ),
                    "average": round(
                        sum(item[0] for item in values) / len(values), 6
                    ),
                }
                for name, values in sorted(category_values.items())
            },
            "tasks": task_rows,
        }


__all__ = ["SkillsBenchAdapter"]
