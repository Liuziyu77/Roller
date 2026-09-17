# Bench integration and scoring

[Documentation home](../index.md) · [Task format reference](../tasks/task-format.md) · [CLI reference](../start/cli-reference.md) · [Trajectory output file guide](../traces/trajectory-outputs.md)

Roller's collection path itself has no notion of "scoring": it destroys the
container after the task finishes. The bench layer adds two things on top:

1. **Translate** a checked-out benchmark into a `roller.task/v1` task list
   (`roller bench export`);
2. After the Agent's last turn and before the container is destroyed, run that
   bench's own grader **in the same container**, and write the score to
   `score.json`.

The bench layer does not change the collection flow. Registered adapters are
listed by `roller bench list`.

## 1. Why the grader must run inside the container

Bench scoring almost always needs files the Agent just wrote (generated scripts,
rendered figures, downloaded data), and `session.finish()` runs
`rm -f <container>`. So the grader cannot run after `collect` returns. Timing
must sit in the middle:

```
session.start() → per-turn send() → [grader runs here] → session.finish() (destroy the container)
```

That is the entire job of `GradedHarness`: wrap an `InteractiveHarness`, replay
its run loop, and insert the grader between the last turn and teardown. The
wrapped harness is unchanged.

Only `InteractiveHarness` can provide a live container, so running a bench on a
non-interactive harness raises `TypeError` at startup, instead of finishing 60
tasks and discovering they all scored 0.

## 2. Three seams: why existing code barely changes

The design rests on three facts confirmed by reading the code:

| Seam | Fact | Result |
| --- | --- | --- |
| `load_tasks()` | Does not reject unknown top-level keys | A task row can carry a `bench` block → `tasks.py` **unchanged** |
| `_finalize_sample` | Copies everything in `HarnessResult.metadata` except three reserved keys into `info.json["runtime"]` | The score floats into the result file by itself → result schema **unchanged** |
| `finish()` | Destroys the container and is memoized | Scoring must happen first → wrap with `GradedHarness` instead of changing the harness |

Existing code needed only two **additive** changes: add `score.json` to the
keep-list in `_prune_sample` (otherwise it is deleted), and add the `bench`
subcommand plus `--task-timeouts` to the CLI.

## 3. The `bench` block (`roller.bench/v1`)

Every task row produced by `bench export` has a `bench` block in addition to the
standard task fields:

```json
{
  "schema_version": "roller.task/v1",
  "id": "03_Social_Interaction_task_2_group_chat_zh",
  "harness": "openclaw",
  "prompt": "……",
  "timeout_seconds": 900,
  "workspace": "/abs/path/to/workspace/03_task2/exec",
  "files": {
    "context": [
      {"role": "skill", "source": "/abs/skills/03_task2", "target": "skills/03_task2"},
      {"role": "fixture", "source": "/abs/workspace/03_task2/tmp", "target": "tmp", "capture": "none"}
    ]
  },
  "runtime": {"env": ["OPENROUTER_API_KEY", "JUDGE_MODEL"], "setup": ["python3 mock_server.py &"]},
  "bench": {
    "schema_version": "roller.bench/v1",
    "name": "wildclawbench",
    "category": "03_Social_Interaction",
    "metadata": {"name": "群聊摘要", "modality": "pure-text", "source_path": "…"},
    "grade": {
      "code": "def grade(transcript, workspace_path):\n    ...",
      "env": ["OPENROUTER_API_KEY", "JUDGE_MODEL"],
      "timeout_seconds": 120,
      "workspace_path": "/tmp_workspace",
      "score_key": "overall_score",
      "stage": [{"source": "/abs/workspace/03_task2/gt", "target": "gt"}]
    }
  }
}
```

### 3.1 `bench.grade` fields

| Field | Default | Description |
| --- | --- | --- |
| `code` | required | Grader Python source. Must contain at least one entry function |
| `entrypoint` | `grade` | Entry function name. Must be a legal identifier |
| `kwargs` | `{}` | Extra arguments passed to the entry function |
| `env` | `[]` | Environment variable names to pass from the host into the grading container |
| `timeout_seconds` | `120` | Grading timeout, 1–3600 |
| `needs_transcript` | `true` | Whether the session record must be fed to the grader |
| `workspace_path` | harness default | Grader working directory (absolute path inside the container) |
| `transcript_path` | derived automatically | Write this only when the harness cannot report the session file location itself |
| `score_key` | `overall_score` | Which key to take from the grader return value as the total score |
| `stage` | `[]` | Host paths copied into the container only before grading; see §4 |

Grader calling convention:

```python
def grade(transcript, workspace_path):   # transcript 仅在 needs_transcript 时传入
    ...
    return {"overall_score": 0.75, "detail": {...}}   # 必须返回 dict
```

A non-dict return is an **explicit error**, not a recorded 0. Roller wraps this
function in a runner of its own, `docker cp`s it into the container, and marks
the score payload on stdout with a `__ROLLER_SCORE__` prefix — graders commonly
print progress and judge traffic, so stdout as a whole cannot be assumed to be
the score.

### 3.2 A grading failure is not a 0 score

Keep these two cases apart, or "the grader broke" will be read as "the model is
weak":

- The grader returns `overall_score: 0.0` normally → a true 0.
- The grader crashed / timed out / produced unparseable output / a `stage`
  source is missing on the host → `score.json` still has a 0 plus an `error`
  field. Aggregation counts it in `unscored_count` and lists it separately in
  the report.

## 4. `stage`: do not put answers in the Agent workspace

Many benches keep ground truth and seed files in the same directory. If you
mount that whole directory into the container, the Agent can read the answers.
Whether the model would look is irrelevant: handing the answer to the subject
under test is itself wrong.

`stage` is a generic host → container delivery: it runs **after the Agent's last
turn and before the grader**. Anything written in `stage` is invisible to the
model for the entire run.

Constraints: `source` must be an absolute host path, and `target` must be a
relative path inside the workspace. A target that starts with `/` or contains
`..` is rejected immediately; otherwise a bench definition could write files
anywhere in the container.

If `source` does not exist on the host, raise an **explicit grading error**
instead of a silent 0: without the answer, the grader would score a correct run
as 0, which looks identical to a model failure.

## 5. Aggregation

`roller bench report <run_dir>` scans samples under the run, reads
`info.json["runtime"]["bench"]`, and aggregates with each bench's
`aggregate()` (default: `mean_aggregate`).

The default metric **divides by the full sample count** (crashes count as 0),
which matches how common benches report officially. It also reports separately:

- `scored_count` / `unscored_count`
- `scored_average` (mean over successfully scored samples only)
- Per-category mean and sample count
- A list of unscored samples and why

Both numbers are reported because the first alone mixes grader failures into the
model score, and the second alone quietly removes crashed samples from the
denominator.

> Note: `bench report` reads `score.json` and **does not** read `info.json`
> `status`. `status: "error"` is often only an SFT export failure (for example
> an image in an observation, where a text-only export would drop data). The
> score on that sample is still valid. Do not use `status` to measure "run
> failure rate."

## 6. Adding a new bench

One subclass plus one registration line. `load()` only translates; it does not
execute anything:

```python
# src/roller/benches/mybench.py
from roller.benches.base import BenchAdapter, BenchBinding, GradeSpec

class MyBenchAdapter(BenchAdapter):
    name = "mybench"
    description = "MyBench — 一句话说明"

    def load(self, root, *, categories=None, task_ids=None):
        rows = []
        for spec in _iter_upstream_tasks(root):
            binding = BenchBinding(
                name=self.name,
                category=spec.category,
                grade=GradeSpec(code=spec.grader_source, env_names=("JUDGE_MODEL",)),
            )
            rows.append({
                "schema_version": "roller.task/v1",
                "id": spec.id,
                "harness": "openclaw",
                "prompt": spec.prompt,
                "timeout_seconds": spec.timeout,
                "bench": binding.as_dict(),
            })
        return rows
```

Then add one line to the registry in `src/roller/benches/__init__.py`. Optionally
override `categories()`, `aggregate()`, and `wrap_harness()` when a task must
replace the execution image or workspace.

A few lessons:

- **Report `categories()` from the actual checkout contents. Do not hard-code
  the upstream category list.** Some checkouts omit categories. A hard-coded
  list makes `--category` select an empty set, which looks like "this bench has
  no tasks."
- **A wrong `--task-id` must error.** A silent empty run looks the same as
  "finished."
- **Parsers should take strings, not `Path`.** "Parse one task file" should be
  unit-testable without the filesystem.
- **Also translate material the upstream runner adds to the prompt.** Prefixes
  and suffixes that are not in the task file and are concatenated by the runner
  (WildClawBench has a 277-character prefix) change the question if you omit
  them.
- **When the upstream has an external dependency that must be wired for a run
  to count as correct, report it at export time.** WildClawBench `web_search`
  hits a local shim, not real Brave. Unwired behavior is that every search
  returns `fetch failed`, the Agent keeps working, and the run still produces
  scores — a silent capability gap is the most expensive kind, because it looks
  identical to "the model does not like to search."

### 6.1 Every bench needs a README

`docs/zh/benches/<name>.md` (English page under `docs/en/benches/`), committed with the adapter. That page does not repeat
generic bench-layer mechanics. It only documents this bench's own configuration
surface:

1. **Shortest path** — a copy-pasteable command sequence from cloning upstream
   to `bench report`;
2. A table of **upstream knobs → Roller equivalents**, listing CLI flags and
   environment variables one by one;
3. **Explicitly mark knobs that were not ported**, and explain why a
   look-alike flag cannot stand in (example: WCB's timeout multiplier rewrites
   the task file and therefore also changes the budget told to the Agent in the
   prompt; `--timeout` only changes wall-clock time and is not equivalent);
4. **Required environment variables**, and whether `--pass-env` is needed;
5. **Known pitfalls**, especially the ones that do not error but make results
   invalid, plus a check command you can run after the job finishes.

Items 2 and 3 are why the page exists: the real risk of integrating a bench is
not a code typo, but an upstream knob that was never ported, while the person
running it does not know it is gone.

## 7. CLI

```bash
# 有哪些 adapter
roller bench list

# 翻译成任务清单（不执行任何东西）
roller bench export wildclawbench /path/to/WildClawBench --output tasks/wcb.jsonl
roller bench export wildclawbench /path/to/WildClawBench -o /tmp/one.jsonl \
    --category 03_Social_Interaction --task-id 03_Social_Interaction_task_2_group_chat_zh

# 采集并评分。--task-timeouts 让每行用自己的 timeout_seconds
roller collect --tasks tasks/wcb.jsonl --model <provider>/<model> \
    --harness openclaw --openclaw-image wildclawbench-ubuntu:v1.3 \
    --task-timeouts --parallel 4 \
    --pass-env JUDGE_MODEL --pass-env OPENROUTER_API_KEY --pass-env OPENROUTER_BASE_URL

# 汇总（run_dir 是位置参数，不是 --run）
roller bench report runs/<run-name>
roller bench report runs/<run-name> --output report.json
```

`--category` and `--task-id` can both be repeated. `--runs-per-task N` emits
N independent rollout rows for each selected upstream task while preserving
the original task id and run index. `--task-timeouts` is the key
switch for a bench run: each bench task carries its own timeout (WildClawBench
has five measured values: 300/360/600/900/1200), while `build_requests`
normally consumes only one global `--timeout`. Without this flag, short tasks
get too much time and long tasks are cut by the wall clock. Being cut looks a
lot like the model cannot do the task.

Environment variables the grader needs must be passed with `--pass-env` (which
variables are declared by the bench's task definition). When a variable is
missing, Roller warns and still continues, so confirm the logs have no
`grading environment variable ... is unset` before a real run.

## 8. Supported benches

Each bench's configuration surface is in its own README (see §6.1). This
section is only an index.

| Bench | `--harness` | README | One-liner |
| --- | --- | --- | --- |
| `wildclawbench` | `openclaw` | [WildClawBench](wildclawbench.md) | 60 real-scenario tasks in 6 categories; the grader runs in the container; `gt/` for 26 tasks goes through `stage` |
| `swebench-pro` | `openclaw` | [SWE-Bench Pro](swebench-pro.md) | 731 software-engineering tasks across 11 repositories; official instance images, hidden tests, and resolved scoring |

Read the matching README's "upstream knobs → Roller equivalents" table and the
"not ported" list before integrating. A missing knob does not error. It only
makes the scores incomparable.
