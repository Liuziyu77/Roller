# Bench 接入与评分

[文档中心](../index.md) · [Task 格式参考](../tasks/task-format.md) · [CLI 参数参考](../start/cli-reference.md) · [轨迹产出文件指南](../traces/trajectory-outputs.md)

Roller 在 task 结束后销毁容器。Bench 层为采集流程增加任务导出和容器内评分：

1. 把已 checkout 的 benchmark 转换为 `roller.task/v1` 任务清单（`roller bench export`）；
2. 在容器销毁前、Agent 最后一轮之后，在同一个容器里执行该 bench 自带的 grader，将分数写入 `score.json`。

`roller collect --bench NAME --bench-root ROOT` 会一键完成转换、采集、评分和聚合；
`bench export` / 普通 `collect` / `bench report` 三步接口仍保留给需要检查或修改中间
task JSONL 的高级流程。已注册的 adapter 见 `roller bench list`。

## 1. 为什么 grader 必须在容器里跑

Bench 的评分几乎都要看 Agent 刚写出来的文件（生成的脚本、渲染的图、下载的数据），
而 `session.finish()` 会 `rm -f <container>`。所以 grader 不能在 `collect` 返回之后跑，
grader 必须在以下时点执行：

```
session.start() → 逐轮 send() → [grader 在这里] → session.finish()（销毁容器）
```

`GradedHarness` 包装 `InteractiveHarness` 并复用其运行循环，在最后一轮和 teardown
之间执行 grader，无需修改被包装的 harness。

只有 `InteractiveHarness` 能在评分时提供仍在运行的容器。使用非交互 harness 运行 bench
会在启动时抛出 `TypeError`，避免完成整批任务后才发现无法评分。

## 2. 三个接缝：为什么现有代码几乎不用动

Bench 层复用以下三个接口：

| 接口 | 行为 | 接入方式 |
| --- | --- | --- |
| `load_tasks()` | 不拒绝未知顶层 key | 任务行可增加 `bench` 块，无需修改 `tasks.py` |
| `_finalize_sample` | 把 `HarnessResult.metadata` 里除三个保留 key 外的内容全拷进 `info.json["runtime"]` | 评分结果随 metadata 写入文件，无需修改结果 schema |
| `finish()` | 销毁容器且做了记忆化 | 用 `GradedHarness` 在销毁容器前执行评分，无需修改 harness |

这些机制是加法式扩展：普通 `--tasks` 采集仍保持原语义；bench 模式额外启用逐题 timeout、
容器内评分、任务快照和自动报告。

## 3. `bench` 块（`roller.bench/v1`）

`bench export` 生成的每一行任务，除标准 task 字段外多一个 `bench` 块：

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

### 3.1 `bench.grade` 字段

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `code` | 必填 | grader 的 Python 源码，至少含一个入口函数 |
| `entrypoint` | `grade` | 入口函数名，必须是合法标识符 |
| `kwargs` | `{}` | 额外传给入口函数的参数 |
| `env` | `[]` | 需要从 host 透传进 grading 容器的环境变量名 |
| `timeout_seconds` | `120` | grading 超时，1~3600 |
| `needs_transcript` | `true` | 是否需要把会话记录喂给 grader |
| `workspace_path` | harness 默认 | grader 的工作目录（容器内绝对路径） |
| `transcript_path` | 自动推导 | 只在 harness 无法自报会话文件位置时才需要写死 |
| `score_key` | `overall_score` | 从 grader 返回值里取哪个键当总分 |
| `stage` | `[]` | 评分前才拷进容器的 host 路径，见 §4 |

grader 的调用约定：

```python
def grade(transcript, workspace_path):   # transcript 仅在 needs_transcript 时传入
    ...
    return {"overall_score": 0.75, "detail": {...}}   # 必须返回 dict
```

返回值不是 dict 时会报错。Roller 用自带 runner 调用该函数，通过 `docker cp` 将
runner 复制进容器执行，并用 `__ROLLER_SCORE__` 前缀标记 stdout 中的分数。
这样可以从 grader 打印的进度和 judge 请求日志中提取评分结果。

### 3.2 grading 失败 ≠ 0 分

评分失败和有效的 0 分分别记录：

- grader 正常返回 `overall_score: 0.0` → 有效的 0 分。
- grader 崩溃 / 超时 / 输出无法解析 / `stage` 的源在 host 上不存在 → `score.json` 里
  除 0 分外还带 `error` 字段，聚合时计入 `unscored_count`，并在报告里单独列出。

## 4. `stage`：答案不能放在 Agent 的工作目录里

很多 bench 将 ground truth 和种子文件放在同一目录。挂载整个目录会让 Agent 能够
读取答案，导致评测信息泄漏。

`stage` 将 host 文件复制到容器，只在 Agent 最后一轮结束后、grader 启动前执行。
因此 Agent 执行任务时无法读取 `stage` 中的文件。

`source` 必须是 host 绝对路径，`target` 必须是 workspace 内的相对路径。
以 `/` 开头或含 `..` 的 target 会被拒绝，防止文件写到工作区之外。

如果 `source` 在 host 上不存在，会报告 grading error。这样可以区分缺少评分文件
和 Agent 答错导致的 0 分。

## 5. 聚合口径

`roller bench report <run_dir>` 扫描 run 下的样本，读 `info.json["runtime"]["bench"]`，
按 bench 各自的 `aggregate()` 汇总（默认是 `mean_aggregate`）。

默认均值以全部样本数为分母，评分失败的样本计 0，与常见 bench 的官方口径一致。报告还会列出：

- `scored_count` / `unscored_count`
- `scored_average`（只对成功评分的样本取均值）
- 每个 category 的均值和样本数
- 未评分样本的清单及原因

全部样本均值包含评分失败的影响；成功评分均值排除了这些样本。报告同时提供两者，
便于判断低分是否与 grader 故障有关。

> `bench report` 根据 `score.json` 统计分数，不使用 `info.json` 的 `status`。
> `status: "error"` 常常只是 SFT 导出失败（例如观察里有图片、纯文本导出会丢数据），
> 这类样本的分数仍然有效，因此不能仅用 `status` 统计评测失败率。

## 6. 加一个新 bench

实现一个 adapter 子类并注册。`load()` 只转换任务定义，不执行任务：

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

然后在 `src/roller/benches/__init__.py` 的注册表里加一行。可选覆盖 `categories()`
和 `aggregate()`（要匹配上游报法时才需要）。

实现时需要处理以下情况：

- `categories()` 从 checkout 的实际内容读取类别。部分 checkout 缺少上游类别，硬编码
  类别列表可能使 `--category` 返回空集。
- `--task-id` 不存在时应报错，避免将空运行误认为成功。
- 解析器接收字符串，不直接接收 `Path`，方便在不访问文件系统的情况下测试解析逻辑。
- 保留上游 runner 添加的 prompt 前缀和后缀，包括未写在任务文件中的内容。
  例如 WildClawBench 有一段 277 字符的前缀，遗漏后会改变任务输入。
- Export 时报告必需的外部依赖。WildClawBench 的 `web_search` 使用本地 shim，
  不直接访问 Brave。未配置时，搜索会返回 `fetch failed`，但 Agent 仍会继续执行并
  产生分数。检查这类错误后，才能判断搜索失败对评测结果的影响。

### 6.1 每个 bench 都要带一个 README

`docs/zh/benches/<name>.md`（英文对应页在 `docs/en/benches/`），和 adapter 一起提交。该页介绍这个 bench 的配置，
通用机制引用本页即可。应包含：

1. 从 clone 上游到 `bench report` 的可复制命令；
2. 上游参数与 Roller 配置的对应表，包括 CLI 参数和环境变量；
3. 尚未支持的上游参数，以及其他参数无法替代它的原因
   （例：WCB 的超时乘数会改写任务文件，因此也改了 prompt 里告诉 Agent 的预算，
   `--timeout` 只改墙钟，不等价）；
4. 必需的环境变量，以及是否需要 `--pass-env`；
5. 已知问题，尤其是不报错却影响结果有效性的情况，并附运行后的检查命令。

参数对应表应明确标出未支持的配置，方便使用者判断 Roller 与上游运行条件是否一致。

## 7. CLI

```bash
# 有哪些 adapter
roller bench list

# 推荐：转换、采集、评分、汇总一条命令完成
roller collect --bench wildclawbench --bench-root /path/to/WildClawBench \
    --bench-category 03_Social_Interaction --model <provider>/<model> \
    --harness openclaw --openclaw-image wildclawbench-ubuntu:v1.3 \
    --parallel 4 --pass-env JUDGE_MODEL --pass-env OPENROUTER_API_KEY

# 高级三步流程：先翻译成任务清单（不执行任何东西）
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

`--category` 和 `--task-id` 都可重复。`--task-timeouts` 是 bench 跑的关键：
bench 的每个任务自带超时（WildClawBench 实测有 300/360/600/900/1200 五档），
而 `build_requests` 平时只吃一个全局 `--timeout`；不加这个开关，
短任务会被给过多时间、长任务会被墙钟砍掉，砍掉的样子跟模型不会做很像。
一键 `collect --bench` 会自动启用逐题 timeout，并在 run 目录保存 `tasks.jsonl` 和
`bench-report.json`，无需再传 `--task-timeouts` 或执行 `bench report`。

grader 需要的环境变量要用 `--pass-env` 透传（哪些变量由 bench 的任务定义声明）。
缺少变量时 Roller 会警告并继续运行。正式采集前，确认日志中没有
`grading environment variable ... is unset`。

## 8. 已支持的 bench

各 bench 的配置见对应 README（见 §6.1）：

| Bench | `--harness` | README | 一句话 |
| --- | --- | --- | --- |
| `wildclawbench` | `openclaw` | [WildClawBench](wildclawbench.md) | 60 个真实场景任务，6 类；grader 在容器里跑，26 个任务的 `gt/` 走 `stage` |
| `skillsbench` | `opencode` | [SkillsBench](skillsbench.md) | 按 task 构建环境；支持 `with-skill` / `no-skill` 对照和官方 verifier |

运行前请核对对应 README 中的参数映射和未支持项。配置缺失不一定触发错误，
却可能改变评测条件，使分数无法直接比较。
