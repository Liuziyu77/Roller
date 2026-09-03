# WildClawBench

[文档中心](../README.md) · [Bench 接入与评分](../bench-format.md) · [CLI 参数参考](../cli-reference.md)

60 个真实场景 OpenClaw 任务，6 个类别。上游：<https://github.com/InternLM/WildClawBench>。

本页只讲**这个 bench 自己的配置面**：上游有哪些旋钮、在 Roller 侧对应什么、哪些没搬过来。
bench 层通用机制（`bench` 块、grading 时序、`stage` 防泄漏、聚合口径）见
[Bench 接入与评分](../bench-format.md)。

## 1. 最短路径

```bash
# 1) 上游 checkout（Roller 不提供 setup 脚本，bench export 就是那层翻译）
git clone https://github.com/InternLM/WildClawBench ~/WildClawBench

# 2) 上游的环境变量照常 source —— Roller 复用同一批变量名，见 §3
set -a; . ~/WildClawBench/.env; set +a

# 3) 翻译成 task 行（不执行、不联网、不起容器）
roller bench export wildclawbench ~/WildClawBench -o /tmp/wcb.jsonl

# 4) 采集 + 判分（--task-timeouts 必须开：每个任务自带超时，见 §4）
roller collect --tasks /tmp/wcb.jsonl --model <provider>/<model> \
  --harness openclaw --openclaw-image wildclawbench-ubuntu:v1.3 \
  --model-config my_api.json --task-timeouts --parallel 4 \
  --output /tmp/wcb --run-name run1 \
  --pass-env NODE_EXTRA_CA_CERTS

# 5) 汇总
roller bench report /tmp/wcb/run1
```

## 2. 上游旋钮 → Roller 对应

`eval/run_batch.py` 的 CLI 参数：

| 上游 | Roller | 备注 |
| --- | --- | --- |
| `--task` / `--category` | `bench export --task-id` / `--category` | Roller 可重复传多个；写错的 id 报错而不是静默跑空 |
| `--model` | `collect --model` | 可重复，多模型笛卡尔展开 |
| `--parallel` | `collect --parallel` | |
| `--agent-backend` | `collect --harness` | adapter 写 `openclaw`；`codex`/`claude-code` 可用 `--harness` 覆盖，`hermesagent` 无对应 |
| `--models-config` | `collect --model-config` | 两边都支持 `${ENV}` 展开 |
| `--thinking` | `collect --thinking` | adapter **不**在 task 里写死 thinking，否则 task 级会盖掉命令行 |
| `--lobster-env` | `collect --pass-env` | |
| `--lobster-name` | `collect --run-name` | 只影响产物目录名 |
| `--global-parallel` | 无（Roller 只有一个全局池） | 相当于恒等于上游 `--global-parallel`；上游默认是按类别串行跑完一池再下一池 |
| `--lobster-workspace` | **未搬** | 上游用它注入 SOUL.md / USER.md 之类的人格工作区。要用的话现在得自己在 task 行里加 `files.context` |
| `--openclaw-image-model` | 部分：`--set-image-model` / `--no-set-image-model` | Roller 只能"用同一个 chat model 或不设"，不能给 image tool 指定**另一个**模型 |

环境变量（`.env`）：

| 上游 | Roller | 备注 |
| --- | --- | --- |
| `DOCKER_IMAGE` | `collect --openclaw-image` | |
| `GATEWAY_PORT` | `collect --gateway-port` | Roller 另有 `--gateway-port-step` 供并行 worker 错开 |
| `TASKS_SUBDIR` | `bench export` 的 `ROOT` 位置参数 | 传 `<root>/tasks_x25` 也可以：`ROOT/tasks` 存在就用它，否则把 `ROOT` 自己当任务目录 |
| `OUTPUT_SUBDIR` | `collect --output` + `--run-name` | |
| `DEFAULT_MODEL` / `DEFAULT_PARALLEL` | 无默认值机制 | 显式传 `--model` / `--parallel` |
| `HTTP_PROXY_INNER` / `HTTPS_PROXY_INNER` / `NO_PROXY_INNER` | 宿主机 `http_proxy` / `https_proxy` / `no_proxy` | OpenClaw harness 默认就透传这些名字，见 §3 |
| `BRAVE_API_KEY` | 同名，默认透传 | 也是 search shim 的 token |
| `SEARCH_SHIM_HOST_IP` / `SEARCH_SHIM_HOSTNAME` / `SEARCH_SHIM_CERT` | **export 时**读同名变量 | 见 §5 |
| `OPENROUTER_API_KEY` / `OPENROUTER_BASE_URL` / `JUDGE_MODEL` | 同名 | 46/60 个任务的 grader 靠它判分，见 §3 |
| `WILDCLAW_TIMEOUT_MULTIPLIER` | **export 时**读同名变量 | 见 §4 |

## 3. 环境变量怎么进容器

三条通路，语义不同：

| 来源 | 缺失时 |
| --- | --- |
| 任务 `## Env` → `runtime.env`（名字清单） | **硬失败**，报 `requires missing environment variable X` |
| OpenClaw harness 默认清单（`OPENROUTER_*`、`BRAVE_API_KEY`、`TAVILY_API_KEY`、大小写两版 proxy） | 静默跳过（值为空就不传） |
| `--pass-env NAME` | 静默跳过 |
| `bench.grade.env`（同样来自 `## Env`） | **只 warn**：`grading environment variable X is unset` |

判分那条通路是 host env 直接 `-e` 进 `docker exec`，不需要额外 `--pass-env`。

⚠️ **判分网关和 judge 模型不同，分数就不可比。** 60 个任务里 46 个的分数是 judge 打的，
所以 `OPENROUTER_BASE_URL` + `JUDGE_MODEL` 是分数的一部分，不是运行细节。
拿两个 run 比分之前先核对这两项。

## 4. 超时

每个任务自带 `timeout_seconds`（实测 300/360/600/900/1200 五档），所以
**`--task-timeouts` 必须开**，否则全部回落到单一的 `--timeout`。

放宽超时用上游同名的环境变量，export 时读：

```bash
WILDCLAW_TIMEOUT_MULTIPLIER=2 roller bench export wildclawbench ~/WildClawBench -o /tmp/wcb_x2.jsonl
```

取值 `ceil(base × multiplier)`，和上游 `tools/run_with_timeout_multiplier.py` 一致；
非法值（非数字、0、负数）直接报错，不会静默按 1× 跑。

⚠️ **不要用 `--timeout` 冒充它。** 上游的乘数是靠改写任务文件里的 `timeout_seconds`
实现的，而**同一个值会被内插进 prompt 前缀**（§6），所以乘完之后"告诉 Agent 的预算"
和真实墙钟仍然一致。`--timeout` 只改墙钟 —— Agent 以为自己有 300 秒，实际有 600 秒。
adapter 这边是在构建任务行之前 scale 的，两者出自同一处，不可能对不上。

## 5. Search：上游不打真 Brave

上游把 `api.search.brave.com` 指向本地一个 **Brave 协议、Serper 后端**的 shim
（`tools/serper_brave_shim.py`，443 端口自签 TLS），靠 `docker run --add-host` 加挂载
证书实现。Roller 没有 `--add-host`、也没有额外挂载参数，所以 adapter 把同一套接线
写成两条 `runtime.setup` 命令（root 身份、在 Agent 之前跑）：

1. 往 `/etc/hosts` 写 `<SEARCH_SHIM_HOST_IP> <SEARCH_SHIM_HOSTNAME>`；
2. 把 `SEARCH_SHIM_CERT` 的内容内联写进
   `/usr/local/share/ca-certificates/wildclaw-search-shim.crt`，跑
   `update-ca-certificates`，并追加进 certifi 的 bundle（requests/httpx 不读系统库）。

这些是 **export 时**读的（和上游同名同默认值，`SEARCH_SHIM_CERT` 的相对路径同样按
checkout 根解析），所以 export 前要先 source `.env`。没配就跳过，只在日志里说一句。

⚠️ **Node 不读系统证书库**，而 OpenClaw 的 `web_search` 是 Node 发的请求。所以还要：

```bash
export NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/wildclaw-search-shim.crt
roller collect ... --pass-env NODE_EXTRA_CA_CERTS
```

漏了这一步的表现是**每次搜索都返回
`{"status":"error","tool":"web_search","error":"fetch failed"}`**，而 Agent 会继续
干活、run 也照样出分 —— 一次静默的能力缺失，不是报错。跑完顺手核一下：

```bash
grep -c '"toolName": "web_search"' <run>/samples/*/*/trace.jsonl
grep -c 'fetch failed'             <run>/samples/*/*/trace.jsonl
```

`web_fetch` 走的是 proxy，和 shim 无关，所以"`web_fetch` 是 200"不能证明搜索是通的。

## 6. Prompt：上游 runner 会拼一段前缀

上游在 `eval/run_batch.py` 里给每个任务的 prompt **前面**拼一段 277 字符
（变量名叫 `system_prompt`，但它不是 system 消息，是拼进 user prompt 的），
里面内插本任务的 `timeout_seconds`。adapter 复现了这一段，所以导出的 prompt 和上游
逐字节一致。漏掉它等于 60 个任务全都在问不一样的问题。

## 7. `## Workspace Path` 不是 Agent 的工作目录

```
<declared>/
├── exec/    # 种子文件 —— 这才是 Agent 的工作目录（60/60 个任务都有）
├── tmp/     # warmup 用的 fixture（5 个任务有）→ files.context，落到 <workspace>/tmp
└── gt/      # ground truth（26 个任务有）→ bench.grade.stage，评分时才进容器
```

挂 `<declared>` 整个目录会同时造成两件事：种子文件全错一层（skill 和 warmup 写
`/tmp_workspace/utils.py`，实际却在 `/tmp_workspace/exec/` 下），以及把 `gt/` 直接
放到 Agent 面前。所以 `workspace` 指向 `<declared>/exec` —— **泄漏是靠目录结构关掉的，
不靠过滤**。

## 8. 段落映射总表

| 上游段落 | 落到 |
| --- | --- |
| `## Prompt` | `prompt`（前面拼上 §6 的前缀） |
| `## Env` | `runtime.env` 和 `bench.grade.env` |
| `## Warmup` | `runtime.setup`（排在 §5 的 shim 接线之后） |
| `## Skills` | `files.context` 的 `role: skill` 条目 |
| `## Workspace Path` | 见 §7 |
| `## Automated Checks` | `bench.grade.code` |
| frontmatter `timeout_seconds` | `timeout_seconds` |

## 9. 已知坑

- **`BRAVE_API_KEY` 不是可选项，缺了 gateway 直接起不来。** 镜像烤进去的
  `openclaw.json` 里 `tools.web.search.apiKey = "${BRAVE_API_KEY}"`，OpenClaw 把它当
  **必需 secret**：解析不到就
  `[WEB_SEARCH_KEY_UNRESOLVED_NO_FALLBACK] … Startup failed: required secrets are
  unavailable`，进程退 1。Roller 侧只看得到
  `OpenClaw gateway exited early with code 1`，而 gateway 日志写在
  `<sample>/rollout/gateway.attempt-N.log`、会被 `_prune_sample` 清掉，所以现场是没有的。
  要复现就手工起一个容器跑 `openclaw gateway --port …` 看 stderr。
  注意它跟"搜索能不能用"是两件事：**值是什么无所谓**（shim token 也行），
  只要非空能解析；真要让搜索通还得配 §5 那套接线。
- **产出物可以很大，跑全量前先算盘子。** 有些任务要求 Agent 把种子文件**整理并复制**到
  交付目录（`01_Productivity_Flow_task_10_pdf_digest` 就是：65 个 PDF 重命名归档），
  实测单个 sample 的 `results/` **658 MB**。60 个任务 × 每个模型都这个量级的话，
  run 目录会比种子本身还大。要省盘就在 `bench report` 之后删 `results/`，
  但别在报完之前删 —— 那是诊断 0 分的唯一现场（见
  [Roller 的 output_dir](../bench-format.md#3-bench-块rollerbenchv1)）。
- **镜像可能把 `http_proxy` 烤进环境变量**（`wildclawbench-ubuntu:v1.3` 就是，且没有
  配套的 `no_proxy`）。发往自建 gateway 的请求会被黑洞，表现是 `request_count=0` 且
  所有任务齐刷刷在同一秒退出 —— 那个整齐的退出时间是代理超时，不是模型慢。
  用 WCB `.env` 里的 `*_INNER` 那组代理（它们的 `NO_PROXY_INNER` 白名单了内网地址）。
  连带后果：**容器里 `curl` 打不通不能当可达性判据**，OpenClaw 的 HTTP 客户端不读
  `http_proxy`，它可能好着。
- **模型的输出预算要覆盖 reasoning。** 会返回 `reasoning_content` 的模型先出推理再出
  正文，`maxTokens` 给小了会拿到 `finish_reason: length` 加空 `content` —— 看起来像
  模型什么都没做。实测同一个模型 `max_tokens=16` 时 `content` 为空、给到 400 才正常
  收尾。所以 `--model-config` 里的 `maxTokens` 要按长任务来配。
- **`SEARCH_SHIM_CERT` 在上游 `.env` 里是相对路径**（相对 WCB checkout 根）。
  Roller 侧 export 时按 `ROOT` 解析；但如果你把它 `--pass-env` 给容器，容器里那个
  相对路径没有意义。
- **`info.json` 的 `status` 不是失败率**。`bench report` 读 `score.json`；
  `status: error` 常见形态有四种，其中三种（观察里有图片、墙钟砍在工具调用中间、
  末轮只有工具调用没有收尾文本）都**照样有有效分**。见
  [Bench 接入与评分 §3.2](../bench-format.md#32-grading-失败--0-分)。
