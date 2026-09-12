# Task format reference

[Documentation home](../index.md) · [Getting started](../start/getting-started.md) · [File lifecycle](task-bundle-and-storage.md) · [Machine-readable schema](../../../schemas/task.schema.json)

Roller uses `roller.task/v1` to describe the conversation, files, Context, and runtime settings needed for one rollout. A Task list can be JSONL or JSON:

- `.jsonl`: each non-empty line is one task.
- `.json`: the top level is a task array, or `{"tasks": [...]}`.

Machine-readable constraints are in the [Task JSON Schema](../../../schemas/task.schema.json).

## 1. Complete single-turn example

```json
{
  "schema_version": "roller.task/v1",
  "id": "product_summary",
  "file_root": "files/product_summary",
  "model": "my-openai-proxy/my-model",
  "prompt": "阅读 inputs/product-brief.md，根据用户偏好写一份摘要，并保存到 outputs/summary.md。",
  "files": {
    "inputs": [
      "inputs/product-brief.md"
    ],
    "context": [
      {
        "role": "user",
        "source": "context/USER.md",
        "capture": "hash"
      },
      {
        "role": "memory",
        "source": "context/MEMORY.md",
        "capture": "hash"
      }
    ],
    "output_dir": "outputs"
  },
  "runtime": {
    "env": ["OPTIONAL_SERVICE_KEY"],
    "setup": ["test -f inputs/product-brief.md"],
    "thinking": "high"
  }
}
```

If the task list is at `examples/tasks/tasks.jsonl`, then `file_root` resolves to:

```text
examples/tasks/files/product_summary/
├── inputs/
│   └── product-brief.md
├── context/
│   ├── USER.md
│   └── MEMORY.md
└── outputs/                   # 逻辑输出目录，可不预先创建
```

When the task runs, the input remains at `inputs/product-brief.md`; files the Agent writes to `outputs/summary.md` end up in the sample as `results/summary.md`.

## 2. Top-level fields

| Field | Required | Description |
| --- | --- | --- |
| `schema_version` | yes | Current version is `roller.task/v1` |
| `id` | yes | Unique string or integer identifier within the Task list |
| `prompt` / `question` / `messages` / `user_simulator` | choose one | Single-turn, compatibility single-turn, predefined multi-turn, or dynamic multi-turn |
| `file_root` | no | Root of the file pack; defaults to the directory that contains the task list |
| `harness` (or `agent_harness` / `task_agent.harness`) | no | Harness used by the Task Agent: `openclaw`, `codex`, `claude-code`, `hermes`, `opencode`, `deepseek`, or `pi`; default `openclaw`. CLI `--harness` overrides all of them |
| `model` | conditionally required | The Task's own model; if the CLI provides `--model`, the CLI model is used |
| `files` | no | Declarations for inputs, Context, and the output directory |
| `runtime` | no | Required environment variables, setup commands, and thinking settings |

Minimal task:

```json
{"schema_version":"roller.task/v1","id":"hello","prompt":"计算 2+3，只回复数字。"}
```

To group Task Agent settings, you can also use
`"task_agent": {"harness": "codex", "model": "provider/model"}`; it is equivalent to the top-level
`harness`/`model`, and repeated declarations must stay consistent. `agent` is a compatibility alias.

If a task has no `model`, the run must provide at least one `--model`. Repeating `--model` runs each task once per model; once the CLI provides models, the task's own `model` is not part of that combination.

## 3. Conversation modes

### 3.1 Single-turn input

```json
{
  "schema_version": "roller.task/v1",
  "id": "single_turn",
  "prompt": "总结当前目录中的项目说明。"
}
```

`question` behaves the same as `prompt` and exists mainly for older data. New tasks should use `prompt`.

### 3.2 Predefined multi-turn

Each element in `messages` is one user-message turn. Roller sends them in order to the same Agent session:

```json
{
  "schema_version": "roller.task/v1",
  "id": "two_turn_review",
  "messages": [
    {
      "role": "user",
      "content": "阅读 inputs/brief.md，先概括主要需求。"
    },
    {
      "role": "user",
      "content": "根据刚才的概括生成最终报告，并保存到 outputs/report.md。"
    }
  ],
  "file_root": "files/two_turn_review",
  "files": {
    "inputs": ["inputs/brief.md"],
    "output_dir": "outputs"
  }
}
```

An element may also be a plain string. The object form currently accepts only `role: "user"`.

Roller does not infer turns from Markdown headings, separators, or other text markers. A message that contains `## Round 1` or `---` is still a single turn.

### 3.3 User Simulator dynamic multi-turn

```json
{
  "schema_version": "roller.task/v1",
  "id": "hr_recruitment_ad",
  "harness": "codex",
  "model": "my-openai-proxy/task-agent-model",
  "user_simulator": {
    "harness": "claude-code",
    "model": "my-openai-proxy/simulator-model",
    "max_turns": 6,
    "instruction": "你是一名 HR。通过多轮交流让任务 Agent 完成招聘广告；检查每版草稿并逐步补充要求，确认最终文件已经保存后结束。"
  },
  "files": {
    "output_dir": "outputs"
  }
}
```

In this mode the task does not predefine user messages. Each turn, the Conversation Manager gives the User Simulator the fixed instruction and the full conversation, and the Simulator decides the next instruction or to finish.

`user_simulator` cannot appear together with `prompt`, `question`, or `messages`. Fields and scheduling details are in [User Simulator and dynamic conversation](user-simulator.md).

## 4. `file_root`

`file_root` is the file-pack root for the current task. Relative paths are resolved from the directory that contains the task list; absolute paths are also allowed for `file_root`, but portable datasets should use relative paths.

For example:

```text
dataset/
├── tasks.jsonl
└── files/
    └── task_001/
        ├── inputs/
        └── context/
```

Corresponding declaration:

```json
{"file_root": "files/task_001"}
```

`files.inputs[].source` and `files.output_dir` are both logical paths relative to that root. Roller does not write Agent output back to the host `file_root`, so parallel models cannot overwrite each other; files are saved under each sample's `results/`.

## 5. Input files

`files.inputs` accepts files or directories:

```json
{
  "files": {
    "inputs": [
      "inputs/brief.md",
      {
        "source": "inputs/documents",
        "required": true
      },
      {
        "source": "inputs/data.csv",
        "required": true,
        "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
      }
    ]
  }
}
```

| Field | Default | Description |
| --- | --- | --- |
| `source` | none | Relative file or directory path under `file_root` |
| `required` | `true` | Whether a missing path fails the task before start |
| `sha256` | none | Optional checksum of the file or directory contents |

Input paths stay the same in the Agent workspace and cannot be renamed with `target`. For example, `inputs/data.csv` appears at:

```text
/tmp_workspace/inputs/data.csv
/roller/task/inputs/inputs/data.csv
```

The first path is suitable to cite directly in a prompt; the second is a stable entry point to the full input snapshot. Files and directories are provided to the Task Agent as read-only.

## 6. Context and system files

`files.context` is for content that needs semantic injection by the Harness:

```json
{
  "files": {
    "context": [
      {"role": "system", "source": "context/SYSTEM.md", "capture": "full"},
      {"role": "user", "source": "context/USER.md", "capture": "hash"},
      {"role": "memory", "source": "context/MEMORY.md", "capture": "hash"},
      {
        "role": "skill",
        "source": "context/skills/web-research",
        "target": "skills/web-research",
        "capture": "full"
      }
    ]
  }
}
```

| Field | Required | Description |
| --- | --- | --- |
| `role` | yes | Semantic role of the Context |
| `source` | yes | Path relative to `file_root`, or an absolute path |
| `target` | no | Relative path after injection; derived from the role when omitted |
| `capture` | no | `full`, `hash`, or `none` |
| `required` | no | Default `true` |

Default targets:

| role | target |
| --- | --- |
| `user` | `USER.md` |
| `memory` | `MEMORY.md` |
| `system` | `SYSTEM.md` |
| `soul` | `SOUL.md` |
| `agents` | `AGENTS.md` |
| `skill` / `skills` | `skills/<source-name>` |

The OpenClaw adapter injects Context into `/root/<target>` and `/tmp_workspace/<target>`. Capture policy and content retention rules are in [Files, Task Bundles, and result storage](task-bundle-and-storage.md#3-context-injection-and-capture).

## 7. Output directory

```json
{
  "files": {
    "output_dir": "outputs"
  }
}
```

`output_dir` defaults to `outputs` and may be a deeper relative path such as `deliverables/final`. Roller creates a writable directory at `/tmp_workspace/<output_dir>/` and links `/roller/task/outputs` to the same location.

After the task finishes, every safe file and empty directory in that folder is copied into the sample's `results/`. You do not and cannot predeclare fixed output filenames; the old field `files.outputs` is no longer supported.

`files.output_dir` and CLI `--output` solve different problems: the former is the collectable directory inside the Agent workspace; the latter is the host root that stores the entire run. For example, `files.output_dir=outputs` with `--output /data/roller-runs --run-name demo` saves the Agent's `outputs/report.md` as `/data/roller-runs/demo/samples/.../results/report.md`.

## 8. Runtime

```json
{
  "runtime": {
    "env": ["EXTRA_API_KEY"],
    "setup": [
      "python3 -m pip install -r inputs/requirements.txt"
    ],
    "thinking": "high"
  }
}
```

| Field | Description |
| --- | --- |
| `env` | Host environment variable names the Task requires; the task fails if any is missing or empty |
| `setup` | Shell commands run in `/tmp_workspace` before the Agent starts |
| `thinking` | Overrides this task's Harness reasoning/thinking setting; for OpenClaw see [Thinking configuration](../harnesses/openclaw-configuration.md#5-thinking-system-prompt-and-wire-trace), for Codex see [Reasoning configuration](../harnesses/codex-configuration.md#4-reasoning-configuration), for Claude Code see [Thinking and effort](../harnesses/claude-code-configuration.md#4-thinking-and-effort) |

`env` and `setup` both accept a single string or an array of strings. Task authors should treat setup as executable code and only run commands from trusted datasets.

The Roller schema for `thinking` accepts `off/minimal/low/medium/high/xhigh/adaptive/max/ultra`;
`none` is normalized to `off`. The value in the task takes precedence over CLI `--thinking`. OpenClaw `2026.7.1` supports
up to `max` and rejects `ultra`; Codex maps valid levels to `model_reasoning_effort` and rejects
the OpenClaw-only `adaptive` and `off`, which has no shared equivalent; the default Codex 0.149.0 image supports
`minimal/low/medium/high/xhigh` and does not support `max/ultra`. Claude Code 2.1.222 accepts
`low/medium/high/xhigh/max/ultracode`; Roller maps `ultra` to `ultracode` and rejects
`off/minimal/adaptive`. The levels that actually work are still validated jointly by the Harness version, model, and provider.
The Task Agent can use OpenClaw, Codex, Claude Code, Hermes, OpenCode, DeepSeek, or Pi; the User Simulator can also choose any of these seven Harnesses in its own `harness` field. Task Agent harnesses may be mixed per task; an explicit `--harness` overrides all of them. The Simulator can choose independently.

## 9. Path and format validation

Roller runs these checks before starting Docker:

- Task IDs must be unique within the list.
- Input paths must be POSIX-style paths relative to `file_root`.
- Inputs and `output_dir` must not use absolute paths, `..`, or globs.
- `output_dir` must not sit under `inputs/` or `context/`, and must not overlap an input or Context target.
- A Context target must not use the reserved top-level directories `inputs`, `outputs`, or `context`.
- `sha256` must be a 64-character hexadecimal string.
- User Simulator `max_turns` must be in `1–100`.

The Task Bundle stage then also checks that files exist and validates type, size, symlinks, and the actual hash.

## 10. Legacy compatibility

Roller can still read some older Interns2_data-style fields:

- `question`
- `workspace` / `workspace_path`
- `persona`
- `warmup`

New data should use `prompt`, `file_root`, `files.inputs`, `files.context`, `files.output_dir`, and `runtime.setup`, so paths, snapshots, and Resume behavior stay explicit.
