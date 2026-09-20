# CLI 参数参考

本页集中列出各 Harness 共用的 Roller CLI 参数，并提供专属参数的指南入口。当前安装版本的参数以命令输出为准：

```bash
roller --help
roller collect --help
roller view --help
```

## 1. 命令结构

```text
roller collect [OPTIONS]   # 采集轨迹
roller view RUN_DIR        # 浏览已有 run
roller bench ...           # bench 翻译与评分汇总，见 §11
roller --version
```

运行 collect 需要 `--tasks`。用一个或多个 `--model` 参数指定模型，也可以在 task 的 `model` 字段中设置：

```bash
roller collect --tasks tasks.jsonl --model provider/model ...
```

## 2. 配置优先级

同一设置同时出现在 task 和命令行时，按以下顺序取值：

| 配置 | 优先级 |
| --- | --- |
| 模型 | 命令行存在任意 `--model` 时使用命令行模型。否则使用每条 task 的 `model` |
| Thinking | task `runtime.thinking` 优先。否则使用 `--thinking`。都省略时交给 Harness/model 默认值 |
| Workspace | task `workspace` 优先。否则使用全局 `--workspace` |
| User Simulator 模型 | `user_simulator.model` 优先。否则跟随当前 task Agent model |

重复的 `--model` 会去重，并与每条 task 做笛卡尔组合。`--start-index` 和 `--limit` 先裁剪规范化后的 task 列表，再展开模型。

## 3. 通用采集参数

以下参数适用于所有 Harness：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--tasks PATH` | 必填 | JSON 或 JSONL task 清单 |
| `--model MODEL` | task `model` | 可重复，为每条 task 运行所有指定模型 |
| `--model-label LABEL` | 实际服务模型 ID | sample 目录和容器名使用的安全本地名称，要求最终只有一个模型，不改变发给上游的模型 ID |
| `--harness NAME` | task `harness` 或 `openclaw` | 选择 `openclaw`、`codex`、`claude-code`、`hermes`、`opencode`、`deepseek` 或 `pi` |
| `--parallel N` | `1` | 最大并行 rollout 数，必须至少为 1 |
| `--grader-parallel N` | `--parallel` | 最大并行 grader 数，可单独降低 judge 服务的请求并发 |
| `--samples N` | SkillsBench 为 `3`，其他为 `1` | 每个 task/model 独立重复次数，每次使用独立 sample 目录 |
| `--retries N` | `0` | 首次失败后的额外尝试次数 |
| `--timeout SECONDS` | `600` | 每条 rollout 的总超时，必须大于 0 |
| `--task-timeouts` | 关闭 | 使用每条 task 的 `timeout_seconds`，缺失时回落到 `--timeout` |
| `--start-index N` | `0` | 从第 N 条规范化 task 开始 |
| `--limit N` | 不限制 | 最多选取 N 条 task |
| `--output DIR` | `runs` | run 根目录的父目录 |
| `--run-name NAME` | UTC 时间名 | 本次 run 目录名 |
| `--resume` | 关闭 | 校验 run lock，并跳过产物完整的成功 sample |
| `--dry-run` | 关闭 | 校验 task 与文件并打印计划，不启动 Docker |
| `--workspace DIR` | 无 | legacy 全局工作区，新 task 使用 `file_root` 和 `files` |
| `--prompt-key NAME` | `prompt` | legacy 数据的 prompt 字段名 |
| `--id-key NAME` | `id` | legacy 数据的 ID 字段名 |
| `--log-level LEVEL` | `INFO` | `DEBUG`、`INFO`、`WARNING` 或 `ERROR` |

文件限制：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--max-input-file-bytes` | 10 GiB | 单个 input/context 文件上限 |
| `--max-task-input-bytes` | 50 GiB | 单条 task 全部输入的总上限 |

## 4. 通用 Harness 运行参数

以下参数控制所有 Harness 共用的容器、推理与轨迹行为。各 Harness 支持的具体取值和额外要求见对应指南。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--docker-image IMAGE` | 对应 Harness 固定镜像 | 覆盖本次运行使用的 Harness 镜像 |
| `--container-user USER` | `0` | 一次性容器用户，`image` 表示沿用镜像默认用户 |
| `--docker-binary PATH` | `docker` | Docker CLI 名称或路径 |
| `--network MODE` | `host` | 容器网络模式，部分 full wire trace 功能依赖 host network |
| `--api-base URL` | Harness 环境变量 | 单一 Harness 运行时的通用 API 地址入口，OpenClaw 通过 model config 设置地址 |
| `--api-key-env NAME` | Harness 默认变量 | 存放模型 API Key 的宿主机变量名 |
| `--pass-env NAME` | 无 | 额外透传一个非空宿主机环境变量，可重复 |
| `--thinking LEVEL` | 未设置 | Harness reasoning/thinking 档位 |
| `--keep-thinking` / `--no-keep-thinking` | 开启 | 控制 ACTF/SFT 是否导出可见 reasoning |
| `--wire-trace MODE` | `actf` | `off`、`actf` 或 `full` |
| `--keep-container-on-error` | 关闭 | 失败时保留已停止的一次性容器便于调试 |

CLI 接受以下 thinking 逻辑值，最终可用子集、默认值和映射由各 Harness 指南说明：

```text
off | minimal | low | medium | high | xhigh | adaptive | max | ultra
```

输入 `none` 会先规范化为 `off`。不受支持的值会在启动 rollout 前报错。

### Wire trace

| 模式 | 产物与用途 |
| --- | --- |
| `off` | 不通过本地反向代理采集 HTTP 请求 |
| `actf` | 默认模式，将实际 tools schema 写入 ACTF，不保留额外 wire 文件 |
| `full` | 额外保留 `wire_trace.jsonl`，用于审计实际 HTTP 请求 |

`full` 可能保存 prompt、reasoning、工具参数和结果等完整请求文本，体积和敏感度都更高。不同 Harness 的支持范围与网络要求见对应 Harness 指南，文件格式见 {ref}`轨迹产出文件指南 <wire-trace-jsonl>`。

### 兼容性提示

`--external-thinking` 已移除。旧命令应改用 `--thinking`，不再需要的参数应直接删除。

## 5. Harness 专属参数索引

镜像、API、Key、执行上限和 Harness 原生配置在对应指南中维护。本页不重复这些默认值，避免同一参数在多处更新后出现差异。

| Harness | 参数与配置指南 |
| --- | --- |
| OpenClaw | {ref}`OpenClaw Harness 指南 <openclaw-cli-flags>` |
| Codex | {ref}`Codex Harness 指南 <codex-cli-flags>` |
| Claude Code | {ref}`Claude Code Harness 指南 <claude-code-cli-flags>` |
| Hermes | {ref}`Hermes Harness 指南 <hermes-cli-flags>` |
| OpenCode | {ref}`OpenCode Harness 指南 <opencode-cli-flags>` |
| DeepSeek | {ref}`DeepSeek Harness 指南 <deepseek-cli-flags>` |
| Pi Agent | {ref}`Pi Agent Harness 指南 <pi-cli-flags>` |

需要核对当前 checkout 的完整参数列表时，运行：

```bash
roller collect --help
```

## 6. Resume

```bash
roller collect [原命令] --resume
```

Resume 会先比对 `lock.json` 中影响运行行为的配置与输入摘要，再验证成功 sample 的必需产物。只有配置和输入一致、产物完整的成功 sample 才会跳过。配置或输入改变会拒绝混用，产物损坏则触发重跑。

应继续同一个 run 时使用 `--resume`。要改变模型、镜像、thinking、wire 或其他语义配置时，应使用新的 `--run-name`。

## 7. Viewer 参数

```bash
roller view RUN_DIR [--host HOST] [--port PORT]
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | 必填 | 包含 `info.json` 与 `samples/` 的 Roller run |
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8765` | 监听端口 |

监听 `0.0.0.0` 会向网络暴露轨迹内容。远程访问使用默认地址和 SSH 端口转发。

## 8. Bench 参数

```bash
roller bench list
roller collect --bench NAME --bench-root ROOT [--bench-category CAT]... [--bench-task-id ID]... [--bench-variant NAME] [--bench-timeout-multiplier N] ...
roller bench export NAME ROOT --output TASKS.jsonl [--category CAT]... [--task-id ID]... [--variant NAME] [--timeout-multiplier N]
roller bench report RUN_DIR [--output REPORT.json]
```

推荐直接使用 `collect --bench`：一条命令完成任务转换、容器执行、评分和聚合，并在
run 目录保存 `tasks.jsonl` 与 `bench-report.json`。它自动采用每条 bench task 的 timeout。
`--bench-category`、`--bench-task-id` 可重复，`--bench-variant` 和
`--bench-timeout-multiplier` 对应下表中的 export 参数。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `NAME` | 必填 | adapter 名，取自 `roller bench list` |
| `ROOT` | 必填 | 已 checkout 的 benchmark 目录 |
| `--output` / `-o` | export 必填 | export 写出的 task JSONL，report 时为可选的报告 JSON |
| `--category CAT` | 不限制 | 只导出该类别，可重复 |
| `--task-id ID` | 不限制 | 只导出该 task，可重复。写错的 id 会报错而不是静默跳过 |
| `--variant NAME` | adapter 默认值 | 选择 bench 条件，例如 SkillsBench 的 `with-skill` / `no-skill` |
| `RUN_DIR` | 必填 | 已完成的 run 目录，**位置参数**（不是 `--run`） |
| `--rebuild-task-images` | 关闭 | 即使 task-specific benchmark 镜像已存在也重新构建 |

`bench export` 只做翻译，不执行任何任务、不联网、不启容器。
`--timeout-multiplier` 必须是有限正数，并在导出时缩放每条任务自己的超时。
手工采用 export + collect 流程时记得给 collect 加 `--task-timeouts`。两种流程都用
`--pass-env` 透传 grader 需要的变量。
`swebench-pro` 的导出保持离线；首次采集某个实例时会拉取官方镜像并构建本地 Harness 派生镜像。

字段语义、评分时序、`stage` 防泄漏规则和聚合口径见 [Bench 接入与评分](../benches/bench-format.md)。
