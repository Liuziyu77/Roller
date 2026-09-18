# SkillsBench

[Documentation](../index.md) · [Bench integration and scoring](bench-format.md) · [OpenCode configuration](../harnesses/opencode-configuration.md)

Roller supports both SkillsBench conditions, `with-skill` and `no-skill`, and runs each
task's own `verifier/test.sh`. Upstream: <https://github.com/benchflow-ai/skillsbench>.

SkillsBench v1.1 does not use an LLM judge: all 87 tasks are scored by their own
`verifier/test.sh`. GPT-5.5 in the upstream README is an agent-under-test model, not a
grading model. Reproducing that entry requires a real GPT-5.5 endpoint for `collect --model`;
a Example model run uses the same verifiers but is a Example model result, not a model-matched reproduction.

## Shortest path

```bash
# Fetch and verify SkillsBench v1.1, then build Roller's OpenCode base image.
scripts/prepare-skillsbench.sh "$PWD/.benchmarks/skillsbench-v1.1"
export SKILLSBENCH_ROOT="$PWD/.benchmarks/skillsbench-v1.1"

set -a
. /path/to/credentials.env
set +a
export ROLLER_RUN_ROOT=$PWD/runs/skillsbench
mkdir -p "$ROLLER_RUN_ROOT"

roller collect --bench skillsbench --bench-root "$SKILLSBENCH_ROOT" \
  --bench-variant with-skill --model example-model \
  --opencode-api-base https://api.example.com/v1 \
  --opencode-api-key-env MODEL_API_KEY --parallel 1 --grader-parallel 1 \
  --output "$ROLLER_RUN_ROOT" --run-name with-skill

roller collect --bench skillsbench --bench-root "$SKILLSBENCH_ROOT" \
  --bench-variant no-skill --model example-model \
  --opencode-api-base https://api.example.com/v1 \
  --opencode-api-key-env MODEL_API_KEY --parallel 1 --grader-parallel 1 \
  --output "$ROLLER_RUN_ROOT" --run-name no-skill
```

The preparation script is idempotent: it leaves an existing checkout untouched, verifies that it
is the pinned v1.1 commit, and checks/builds the harness base image again. Its argument may be any
persistent directory, and it neither reads nor writes API keys. The equivalent manual procedure is
to clone the `v1.1` tag from <https://github.com/benchflow-ai/skillsbench> and build
`docker/opencode`.

Each command automatically writes `tasks.jsonl` and `bench-report.json` inside its run.
For SkillsBench, the one-command defaults are 3 trials per task, 100 OpenCode
steps per trial, and 32768 output tokens per model response; `--samples`,
`--opencode-max-steps`, and `--opencode-output-tokens` can override them.
Pin official comparisons to tag `v1.1` (peeled commit
`b63b7b2850226b6aa4fb5929a8c1ac7bc4d9a6af`) rather than a moving `main` checkout.

Start with `--bench-task-id 3d-scan-calc` as a one-task smoke test. Both
`--bench-category` and `--bench-task-id` are repeatable. For a slower model, add
`--bench-timeout-multiplier 2`; this scales both the agent and verifier budgets.
Use `--samples N` for a non-standard repeat count.

## What Roller does

- Exports the ordered set in `integrations/agentbeats/task_sets/skillsbench-v1.1.json`,
  falling back to scanning `tasks/` when the set is absent.
- Builds each `environment/Dockerfile`, then layers the pinned OpenCode harness onto it.
  Image tags contain an environment digest and are reused; pass `--rebuild-task-images`
  to force a rebuild.
- Uses the final Dockerfile `WORKDIR` as the Agent workspace.
- Removes Dockerfile skill copies from the base environment. `with-skill` injects
  `environment/skills/` at runtime, while `no-skill` clears supported skill lookup paths.
  The two conditions therefore share the same task environment and differ only by skills.
- Stages `verifier/` only after the Agent finishes, runs `/verifier/test.sh`, and reads
  `/logs/verifier/reward.txt`; verifier files are not exposed to the Agent.
- Reports upstream-style score eligibility: agent timeouts/errors and verifier or
  infrastructure errors remain visible but are excluded from pass rate and mean reward.

A task's first run may spend substantial time downloading dependencies. Docker builds, task
containers, and verifiers inherit `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` from the current
shell without storing their values in the exported task list. Add private model endpoints to
`NO_PROXY`. Verifiers retry transient download or DNS failures that occur before tests start;
samples that still fail remain score-ineligible. Put real run output on persistent shared storage.

## Boundaries

The adapter targets upstream's default `tasks/` set and does not automatically include
`tasks-extra/`. Declared taxonomy, CPU, memory, storage, GPU, network, and OS values are retained
in bench metadata but are not currently translated into Docker
resource limits; enforce them outside Roller when strict resource comparability matters.
For the few upstream compose definitions, their verifiers support starting the needed service
inside the main image, so Roller builds the main Dockerfile.
