# Bench 接入与评分

[文档中心](../index.md) · [Task 格式参考](../tasks/task-format.md) · [CLI 参数参考](../start/cli-reference.md) · [轨迹产出文件指南](../traces/trajectory-outputs.md)

Roller 的采集路径本身不含"评分"概念：它跑完 task 就把容器销毁。Bench 层在这之上补两件事：

1. 把一个 checkout 好的 benchmark **翻译**成 `roller.task/v1` 任务清单（`roller bench export`）；
2. 在容器销毁前、Agent 最后一轮之后，在**同一个容器里**执行该 bench 自带的 grader，把分数落到 `score.json`。

Bench 层不改采集流程。已注册的 adapter 见 `roller bench list`。

## 1. 为什么 grader 必须在容器里跑

Bench 的评分几乎都要看 Agent 刚写出来的文件（生成的脚本、渲染的图、下载的数据），
而 `session.finish()` 会 `rm -f <container>`。所以 grader 不能在 `collect` 返回之后跑，
时序上必须卡在中间：

```
session.start() → 逐轮 send() → [grader 在这里] → session.finish()（销毁容器）
```

这就是 `GradedHarness` 的全部职责：包住一个 `InteractiveHarness`，复刻它的运行循环，
在最后一轮和 teardown 之间插入 grader。被包住的 harness 一行没改。

只有 `InteractiveHarness` 能提供活着的容器，所以拿非交互 harness 去跑 bench 会在启动时
就抛 `TypeError`，而不是跑完 60 个任务才发现全是 0 分。

## 2. 三个接缝：为什么现有代码几乎不用动

整个设计建立在读代码确认的三件事上：

| 接缝 | 事实 | 结果 |
| --- | --- | --- |
| `load_tasks()` | 不拒绝未知顶层 key | 任务行可以塞一个 `bench` 块 → `tasks.py` **0 改动** |
| `_finalize_sample` | 把 `HarnessResult.metadata` 里除三个保留 key 外的内容全拷进 `info.json["runtime"]` | 评分结果自己浮到结果文件里 → 结果 schema **0 改动** |
| `finish()` | 销毁容器且做了记忆化 | 评分必须前置 → 用 `GradedHarness` 包一层，而不是改 harness |

对既有代码只有两处**加法**改动：`_prune_sample` 的保留名单里加 `score.json`（不加会被清掉），
以及 CLI 增加 `bench` 子命令和 `--task-timeouts`。

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

返回非 dict 会**显式报错**而不是记 0 分。Roller 把这个函数包进一个自带 runner，
`docker cp` 进容器执行，用 `__ROLLER_SCORE__` 前缀标记 stdout 里的分数载荷 ——
因为 grader 普遍会打印进度和 judge 往返，不能假设 stdout 整体就是分数。

### 3.2 grading 失败 ≠ 0 分

区分这两件事很重要，否则"grader 坏了"会被读成"模型很弱"：

- grader 正常返回 `overall_score: 0.0` → 真的 0 分。
- grader 崩了 / 超时 / 输出无法解析 / `stage` 的源在 host 上不存在 → `score.json` 里
  除 0 分外还带 `error` 字段，聚合时计入 `unscored_count`，并在报告里单独列出。

## 4. `stage`：答案不能放在 Agent 的工作目录里

很多 bench 的 ground truth 和种子文件放在同一个目录下。如果照搬整个目录挂进容器，
Agent 就能读到答案 —— 这跟"模型会不会去看"无关，评测把答案交给被测对象本身就是错的。

`stage` 是通用的 host → 容器投递：**Agent 最后一轮结束之后、grader 之前**才执行。
写进 `stage` 的东西模型全程看不到。

约束：`source` 必须是 host 绝对路径，`target` 必须是 workspace 内的相对路径 ——
以 `/` 开头或含 `..` 的 target 直接拒绝，否则一份 bench 定义就能往容器任意位置写文件。

如果 `source` 在 host 上不存在，报**明确的 grading error**而不是静默 0 分：
grader 拿不到答案会把做对的 run 判 0，读起来跟模型失败一模一样。

## 5. 聚合口径

`roller bench report <run_dir>` 扫描 run 下的样本，读 `info.json["runtime"]["bench"]`，
按 bench 各自的 `aggregate()` 汇总（默认是 `mean_aggregate`）。

默认口径**除以全部样本数**（崩掉的算 0），与常见 bench 的官方报法一致；同时单独给出：

- `scored_count` / `unscored_count`
- `scored_average`（只对成功评分的样本取均值）
- 每个 category 的均值和样本数
- 未评分样本的清单及原因

两个数都报，是因为只报前者会把 grader 故障混进模型分，只报后者会把崩掉的样本
从分母里偷偷拿掉。

> 注意：`bench report` 读的是 `score.json`，**不读** `info.json` 的 `status`。
> `status: "error"` 常常只是 SFT 导出失败（例如观察里有图片、纯文本导出会丢数据），
> 这类样本的分数是有效的。要统计"跑失败率"别用 `status`。

## 6. 加一个新 bench

一个子类加一行注册，`load()` 里只做翻译，不执行任何东西：

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

几条经验：

- **`categories()` 从 checkout 实际内容里报，不要硬编码上游的类别列表。** 部分 checkout
  会少类别，硬编码会让 `--category` 筛出空集，跑出来像"这个 bench 没任务"。
- **`--task-id` 写错要报错。** 静默的空运行看起来跟"跑完了"一样。
- **解析器收字符串而不是 `Path`。** 让"解析一个任务文件"能脱离文件系统被单测。
- **上游 runner 加进 prompt 的东西也要翻译。** 不在任务文件里、由 runner 拼上去的
  前缀/后缀（WildClawBench 就有一段 277 字符前缀）漏掉的话，等于在问不一样的问题。
- **上游有"必须配好才算跑对"的外部依赖时，要在 export 时报出来。** WildClawBench 的
  `web_search` 打的是本地 shim 而不是真 Brave，没接线的表现是每次搜索都返回
  `fetch failed`、而 Agent 继续干活、run 照样出分 —— 静默的能力缺失最贵，因为它
  长得跟"模型不爱搜"一模一样。

### 6.1 每个 bench 都要带一个 README

`docs/benches/<name>.md`，和 adapter 一起提交。这一页不重复 bench 层的通用机制，
只写这个 bench 自己的配置面：

1. **最短路径** —— 从 clone 上游到 `bench report` 的可复制命令序列；
2. **上游旋钮 → Roller 对应**的表，逐项列 CLI 参数和环境变量；
3. **明确标出"未搬"的旋钮**，并说明为什么不能用某个看起来等价的参数冒充
   （例：WCB 的超时乘数会改写任务文件，因此也改了 prompt 里告诉 Agent 的预算，
   `--timeout` 只改墙钟，不等价）；
4. **必需环境变量**，以及要不要 `--pass-env`；
5. **已知坑**，尤其是"不报错但结果无效"的那些，附一条跑完就能查的核对命令。

第 2、3 条是这页存在的理由：接入一个 bench 的实际风险不是代码写错，而是上游某个
旋钮没搬过来、跑的人也不知道它不在了。

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

`--category` 和 `--task-id` 都可重复。`--task-timeouts` 是 bench 跑的关键：
bench 的每个任务自带超时（WildClawBench 实测有 300/360/600/900/1200 五档），
而 `build_requests` 平时只吃一个全局 `--timeout`；不加这个开关，
短任务会被给过多时间、长任务会被墙钟砍掉，砍掉的样子跟模型不会做很像。

grader 需要的环境变量要用 `--pass-env` 透传（哪些变量由 bench 的任务定义声明）。
缺变量时 Roller 会 warn 但仍然继续跑，所以真跑之前先确认日志里没有
`grading environment variable ... is unset`。

## 8. 已支持的 bench

每个 bench 的配置面写在自己的 README 里（见 §6.1），这里只做索引。

| Bench | `--harness` | README | 一句话 |
| --- | --- | --- | --- |
| `wildclawbench` | `openclaw` | [WildClawBench](wildclawbench.md) | 60 个真实场景任务，6 类；grader 在容器里跑，26 个任务的 `gt/` 走 `stage` |

接入前先读对应 README 的"上游旋钮 → Roller 对应"表和"未搬"清单 ——
少一个旋钮不会报错，只会让分数不可比。
