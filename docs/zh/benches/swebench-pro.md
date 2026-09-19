# SWE-Bench Pro

[文档中心](../index.md) · [Bench 接入与评分](bench-format.md) · [CLI 参数参考](../start/cli-reference.md)

Roller 支持 SWE-Bench Pro 公共 test split 的 731 个任务、11 个仓库，并复用官方
Docker 镜像、`run_script.sh`、`parser.py`、F2P/P2P 集合和 resolved 判定。
上游：<https://github.com/scaleapi/SWE-bench_Pro-os>。

## 1. 数据与导出

本机已准备的数据根目录是 `/xxx/SWE-bench_Pro`，其中包含 `test.jsonl`、
`test.parquet`、`test_patches/` 和官方 `run_scripts/`。

```bash
roller bench export swebench-pro /xxx/SWE-bench_Pro \
  --output /tmp/swebench-pro.jsonl

roller bench export swebench-pro /xxx/SWE-bench_Pro \
  --task-id instance_flipt-io__flipt-a0cbc0cb65ae601270bdbe3f5313e2dfd49c80e4 \
  --output /tmp/swebench-pro.jsonl
```

`--category` 使用仓库名，例如 `flipt-io/flipt`。Adapter 优先读取 `test.jsonl`；
若数据根目录只有 Parquet，运行 `pip install -e '.[swebench-pro]'`。

## 2. 一键运行脚本

`scripts/run-swebench-pro.sh` 会自动导出任务、采集轨迹并生成
`<OUTPUT_ROOT>/<RUN_NAME>/report.json`。建议先指定一个实例做 smoke：

```bash
export MY_PROXY_BASE_URL=https://api.example.com/v1
export MY_PROXY_API_KEY=replace-me

SWEBENCH_PRO_ROOT=/path/to/SWE-bench_Pro \
MODEL=my-openai-proxy/my-model \
MODEL_CONFIG=examples/config/openclaw/models.json \
TASK_IDS=instance_flipt-io__flipt-a0cbc0cb65ae601270bdbe3f5313e2dfd49c80e4 \
./scripts/run-swebench-pro.sh
```

`HARNESS` 默认为 `openclaw`。`TASK_IDS` 和 `CATEGORIES` 接受逗号分隔列表；
`PARALLEL`、`RETRIES`、`OUTPUT_ROOT` 和 `RUN_NAME` 可覆盖默认值。使用其他
Harness 时设置 `HARNESS`、`API_BASE` 和 `API_KEY_ENV`，其中
`API_KEY_ENV` 是保存 Key 的变量名，不是 Key 本身。可选的 `MAX_STEPS` 会映射到
Claude Code、Hermes、OpenCode、DeepSeek 和 Pi 对应的步数参数。脚本后的参数会
追加给 `roller collect`，例如 `--resume`。

## 3. Harness 与模型 API

```bash
export MY_PROXY_BASE_URL=https://api.example.com/v1
export MY_PROXY_API_KEY=replace-me

roller collect --tasks /tmp/swebench-pro.jsonl \
  --model my-openai-proxy/my-model \
  --harness openclaw \
  --model-config examples/config/openclaw/models.json \
  --task-timeouts --parallel 1 \
  --output /tmp/swebench-pro-runs --run-name local-qwen
```

Roller 支持 OpenClaw、Codex、Claude Code、Hermes、OpenCode、DeepSeek 和 Pi。
导出的 task 默认写入 `harness: openclaw`，可用 `--harness` 覆盖。Codex 使用
Responses API，Claude Code 使用 Anthropic Messages API，其余非 OpenClaw Harness
使用 OpenAI-compatible Chat Completions API；相应 endpoint 和 key 参数见
[CLI 参数参考](../start/cli-reference.md)。

每个任务第一次运行时，Roller 会拉取
`jefzda/sweap-images:<dockerhub_tag>` 并与所选 Harness 镜像组合。OpenClaw 的运行时
被放入官方实例镜像；其余 Harness 以 Harness 镜像为运行环境，并从官方实例镜像复制
`/app` 仓库。派生结果按源镜像、Harness 镜像和 Harness 名称缓存。首次运行需要较多
网络、磁盘和启动时间，后续会复用本地镜像。

## 4. 隔离与评分

Agent 在派生镜像的 `/app` 中工作。启动时 Roller reset 到 `base_commit`，记录原仓库
tracked files，删除上游 `.git` 历史，再创建只有基线快照的新仓库。因此 Agent 无法
从 Git 历史恢复 gold commit，导出任务也不包含 gold source patch。对非 OpenClaw
Harness，Agent 的系统依赖来自 Harness 镜像，可能与官方实例镜像不同。

Agent 最后一轮结束后，Roller 先提取 tracked、untracked 和已 commit 的模型改动为
`model.patch`，去掉官方同样不接受的 binary hunks，再启动一个全新的官方实例镜像
容器。只有这个独立容器会接收模型补丁、隐藏 `test.patch`、官方 runner 和 parser。
grader 在干净基线上应用模型补丁，再用隐藏版本覆盖官方测试文件。无论评分成功或
失败，独立评分容器都会被删除。只有 F2P 与 P2P 测试的并集全部为 `PASSED` 时才算
resolved，`overall_score` 为 1，否则为 0。

每个 sample 的 `results/` 包含 `model.patch`、`test_stdout.log`、
`test_stderr.log` 和 `test_output.json`；分数位于 sample 根目录的 `score.json`。

```bash
roller bench report /tmp/swebench-pro-runs/local-qwen
```

## 5. 可比性边界

- 必须使用全新实例容器评分；不要复用手工修改过的容器。
- 必须加 `--task-timeouts`；每题 Agent 预算为 3600 秒，grader 另有 3600 秒上限。
- 当前使用本地 Docker，不实现上游 Modal 后端。
- 七个 Harness 共用相同的全新官方镜像评分路径。
- 默认保留容器网络，不等同于上游 `--block_network`。
- 完整运行会拉取大量按实例区分的镜像；先用 `--task-id` 做 smoke 并监控磁盘。

## 6. 可选环境与评分修正

OpenClaw 的 Node 位于 `/opt/roller-node/bin/node`，项目原有的 Node 保留不变。
Python API `SWEBenchProAdapter` 另提供三个默认关闭的选项，导出的 manifest 会记录其值：

- `go_proxy="https://goproxy.cn,direct"`：保留镜像的 Go 缓存路径并补齐登录 shell 的 Go 路径。若测试报告缺少 go.sum，下载依赖后在原评分时间预算内重新测试；依赖改动和首次失败日志单独保存。
- `repair_test_fixtures=True`：从上游 gold patch 中仅提取测试数据目录的非执行文本数据，评分时补齐遗漏的 fixture。该内容不会进入 Agent 工作区或提示词。
- `exclude_agent_tests=True`：评分时排除模型对 Go/Python/JS 测试源码的改动，避免新增自测与隐藏测试重名。原始 `model.patch` 保留，实际用于评分的补丁另存为 `graded.patch`。

这些选项不改变官方通过标准，仅调整运行环境与评分基础设施。实验报告应同时保留原始成绩和修正后成绩。仅重跑失败任务所得的累计结果不能当作独立 pass@1。
