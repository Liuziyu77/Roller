# 文件、Task Bundle 与结果存储

本文说明一条 task 从宿主机文件变成 Agent 工作区，再变成最终 sample 的完整过程。Task 字段本身见 [Task 格式参考](task-format.md)。

## 1. 文件生命周期

Roller 把文件分成四个阶段：

1. **宿主机文件包**：`file_root` 下由用户维护的输入与 Context。
2. **Task Bundle**：运行前构建的不可变快照，供同一 task 的所有模型使用。
3. **Agent 工作区**：每个 rollout 独享的 `/tmp_workspace`。
4. **Sample**：最终保留 raw trace、ACTF、info 和可选 `results/`。

一组典型路径如下：

| 用途 | 路径示例 |
| --- | --- |
| Task 清单 | `dataset/tasks.jsonl` |
| `file_root` | `dataset/files/task_001/` |
| 宿主机输入 | `dataset/files/task_001/inputs/brief.md` |
| Agent 读取 | `/tmp_workspace/inputs/brief.md` |
| Agent 写入 | `/tmp_workspace/outputs/report.md` |
| 最终结果 | `samples/<model>/00000000_task_001/results/report.md` |

`file_root` 定义逻辑工作区关系，但 Roller 不会把 Agent 生成的文件直接写回它。每个 task/model rollout 都有自己的容器和 sample，因此并行模型不会覆盖彼此的输出。

## 2. 输入快照

`files.inputs` 中的文件和目录在 Docker 启动前扫描。Roller 会：

- 拒绝符号链接、特殊文件和路径逃逸。
- 递归记录目录结构。
- 计算每个文件的 SHA-256、大小和权限。
- 校验声明的可选 `sha256`。
- 把相同内容保存为可复用 blob，并构建内容寻址的输入树。

默认限制：

| 限制 | 默认值 | CLI |
| --- | ---: | --- |
| 单个输入或 Context 文件 | 10 GiB | `--max-input-file-bytes` |
| 单条 task 输入总量 | 50 GiB | `--max-task-input-bytes` |

同一 run 中内容相同的输入树可以复用。Task Bundle manifest 记录声明、文件 hash、输入树 digest 和 Context digest；最终 sample 只保存这些 digest，不复制整套输入依赖。

OpenClaw 中的两个入口：

```text
/tmp_workspace/<source>          # 每个声明输入保持自己的相对路径
/roller/task/inputs/<source>     # 完整输入快照的只读入口
```

Task Bundle 位于运行期临时目录，采集结束后删除。使用 `--resume` 时，Roller 会重新读取当前 task 和文件，重建 digest 后再判断能否复用旧 sample。

## 3. Context 注入与 capture

Context 和普通输入的区别在于：普通输入只按路径提供；Context 还带有 `role` 与 `target`，由 Harness 放到能够被 Agent 识别的位置。

OpenClaw adapter 会把 Context 复制到：

```text
/root/<target>
/tmp_workspace/<target>
```

因此 `role: "user"` 的默认 `USER.md`、`role: "memory"` 的默认 `MEMORY.md`，以及 skills 目录都能按 OpenClaw 的常用约定被发现。

`capture` 决定 Task Bundle 如何记录 Context 内容：

| 策略 | 记录 hash | 保存内容寻址 blob | 允许安全 Resume |
| --- | --- | --- | --- |
| `full` | 是 | 是 | 是 |
| `hash` | 是 | 否 | 是 |
| `none` | 否 | 否 | 否 |

三种策略都会把 Context 提供给当前 rollout。区别只影响运行期 Bundle 的内容留存和之后的 Resume 判断；最终紧凑 sample 不保存 Context 原文。

默认情况下，`user` 和 `memory` 使用 `hash`，其他 role 使用 `full`。包含隐私内容但仍需可靠检测变化时，可以使用 `hash`；完全不希望记录内容 hash 时使用 `none`，同时接受该 task 无法安全 Resume。

## 4. Agent 工作区

每个 OpenClaw rollout 都使用独立容器和 `/tmp_workspace`：

```text
/tmp_workspace/
├── inputs/                    # 只读挂载的声明输入
├── USER.md                    # 可选 Context
├── MEMORY.md                  # 可选 Context
├── skills/                    # 可选 Context
└── outputs/                   # 默认可写输出目录
```

输入文件按各自 source 路径只读挂载。Context 在运行前注入。`runtime.setup` 随后在 `/tmp_workspace` 执行，最后才开始 Agent 对话。

`/roller/task/outputs` 是 task `output_dir` 的稳定别名。即使 task 使用 `deliverables/final`，该别名仍指向当前可回收目录。

Legacy workspace 模式会把整个旧 workspace 快照复制到工作区；新数据应显式声明输入和 Context，以减少无关文件并提高 digest 的可解释性。

## 5. 输出回收

Task 只需要声明一个目录：

```json
{
  "files": {
    "output_dir": "outputs"
  }
}
```

任务结束后，Roller 遍历整个 `output_dir`，把所有普通文件和空目录复制到 sample 的 `results/`，并保持相对结构：

```text
/tmp_workspace/outputs/
├── report.md
└── assets/chart.png

results/
├── report.md
└── assets/chart.png
```

每个结果文件会在 sample `info.json` 中记录：

- 相对路径
- SHA-256
- 字节数
- MIME type

输出为空时不创建 `results/`。发现符号链接或特殊文件时，安全的普通文件仍可被保存，但 sample 状态会变为 `error`，以免不完整或危险的输出被当作成功结果。

`--collect-path` 仍可把旧任务指定的额外容器路径复制到 `results/legacy/`。新 task 应让 Agent 把需要保留的文件写入 `output_dir`。

## 6. Run 与 Sample

普通 rollout 的最终目录：

```text
<output>/<run-name>/
├── info.json
└── samples/
    └── <model>/
        └── <index>_<task>/
            ├── trace.jsonl
            ├── trace_ACTF.json
            ├── info.json
            └── results/                 # 可选
```

Run `info.json` 保存：

- 本次命令的重要配置
- task、模型与 trajectory 数量
- 成功、失败、超时和跳过数量
- 全部 sample 的 usage 汇总
- sample 相对路径索引

Sample `info.json` 保存：

- task ID、模型、Harness 和状态
- 错误与 attempt 历史
- token、cache、cost 和请求数
- 开始时间、结束时间和耗时
- OpenClaw 版本、镜像 digest、脱敏 API base
- Task Bundle、输入与 Context digest
- 输出文件清单
- raw/ACTF 路径

运行期 gateway/agent 日志、临时模型配置、Task Bundle、retry 中间 trace 和未回收的工作区文件不会留在最终 sample。

User Simulator sample 会增加：

```text
<index>_<task>/
├── conversation.json
└── user_simulator/
    ├── trace.jsonl
    ├── trace_ACTF.json
    └── info.json
```

根 sample 仍代表任务 Agent；`user_simulator/` 代表模拟用户。双方 usage 的总和写入根 `info.json`，分项写入该文件的 `conversation.participants`。详情见 [User Simulator 与动态对话](user-simulator.md#8-保存与使用轨迹)。

## 7. Raw Trace 与 ACTF

### `trace.jsonl`

这是 Harness 原始事实来源。Roller 不删除 tool call、不改写消息内容，也不把多个事件合并成自定义文本格式。

### `trace_ACTF.json`

ACTF 文件直接保存 `ACTF_v1.0` trajectory object：

```json
{
  "schema_version": "ACTF_v1.0",
  "steps": [],
  "started_at": "2026-08-20 10:00:00.000000",
  "finished_at": "2026-08-20 10:00:01.000000"
}
```

转换规则：

- 每个 assistant message 形成一个 step。
- 可见回复进入 `assistant_content.content`。
- reasoning/thinking 进入 `assistant_content.reasoning_content`。
- Tool call 同时写入 `assistant_content.tool_calls` 和 `tools`。
- 随后的 tool result 进入该 step 的 `observation`。
- 当前 user message 写入 `user_content`。
- OpenClaw usage 映射到 step `metric`。

如果 Harness 成功结束但没有可用 raw trace，sample 会标记为 `error`。ACTF 转换失败同样不会被当作成功 sample。

## 8. 安全与隐私边界

### 文件

- 输入和 Context 拒绝符号链接与特殊文件。
- 路径不允许使用 `..` 逃逸声明范围。
- 输出只回收 `output_dir`，不保存工作区中的其他文件。
- 输出中的符号链接和特殊文件不会被复制。

### API Key

- `${ENV_NAME}` 只在运行时展开。
- OpenClaw 临时模型配置在系统临时目录创建，注入容器后删除。
- Run metadata 只记录环境变量名称，不记录值。
- API base 会移除认证信息、query 和 fragment 后再保存。

不要把真实 Key 写入仓库中的 task、JSON 示例或 README。推荐使用被 Git 忽略的本地 `.env` 或外部 secret manager。

### Context

`capture=hash` 仍会保存不可逆内容 hash；如果连 hash 都不能保留，应使用 `capture=none`。这样做会关闭该 task 的安全 Resume，因为 Roller 无法确认 Context 是否变化。

## 9. Retry 与 Resume

`--retries N` 在同一 sample 中最多额外执行 N 次。最终 `info.json` 保存每次 attempt 的状态、错误与耗时；canonical raw/ACTF 代表最后一次 attempt，运行期中间文件会被清理。

`--resume` 只复用满足全部条件的 sample：

1. 旧状态为 `ok`。
2. `trace.jsonl` 与 `trace_ACTF.json` 都存在。
3. 当前 Task Bundle digest 与旧 sample 一致。
4. Context 没有使用 `capture=none`。
5. User Simulator task 还必须有完整的 `conversation.json` 和 Simulator raw/ACTF。

以下变化会导致重新运行：

- prompt、messages 或 User Simulator 配置变化
- 输入内容、目录结构或声明变化
- Context 内容、target 或 capture 变化
- `output_dir` 变化
- Runtime env、setup 或 thinking 变化

模型变化会进入不同的 sample model 目录，不会覆盖原模型结果。
