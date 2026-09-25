# Chart2Code 旁路开发状态

此分支继承通用候选提交 `7cd8a56`。当前完成了可离线验证的任务验收、生命周期保护与导出核心，**生产模型入口、服务矩阵及真实 canary 尚未完成**。不能把合成回归当作真实采集已验收。

## 分支边界与当前入口

`roller_chart2code` 包拥有 recorder 验证、profiles、三门验收、pipeline 和 exporter。`docker/chart2code/` 拥有 recorder。通用 `roller` 包未增加任务导入、专属钩子或判断分支；原有 `roller collect` 仍只生成通用候选 SFT，不能作为 Chart2Code 合格数据入口。

旁路 `pipeline.Chart2CodeCollector` 是薄子类，不复制主线调度或转换器。调用方须先用 `TaskBundleStore` 准备不可变输入，再用 `contract_for()` 从实际请求冻结 prompt、参考图 SHA256、完整样本身份和 full-wire 要求；传入 `execution` 的必须是已经解析的模型/Harness/端点/镜像配置，而非任意实验名。生产 launcher 将负责这一接线，本阶段不提供可能绕过保护的临时模型启动命令。

生命周期保证：

1. 全部待复用样本在任何 Harness 调用和通用 run 元数据更新前验证。配置锁、输入 bundle、通用完整性、任务验收均必须通过；没有可信 run lock 的历史样本不自动接管。
2. 旧报告缺失/过期不会触发付费重跑；证据完好则重新离线验收。坏样本停止整次 resume，保留原目录。新采集默认零重试，显式 retries 沿通用 Collector；timeout 是每 attempt 上限。对旧样本另行重跑仍应选择新 run 目录。
3. 主线完成采集/转换后，任务层写独立报告及 summary，不把 `info.json.status` 改写成任务结论。复用的 run 行保留 `skipped`，样本原始状态保持 `ok`。
4. 汇总重新审计 ACTF/SFT/全部归档证据。只导出合格样本至新的数据集目录；失败/未决样本在 manifest 中列出原因。零合格样本不发布。异常发布保留 `.staging-*` 目录供排查，不清理历史数据。

协作进程锁用于串行化同一任务 run 的旁路生命周期。请勿同时用原生 `roller collect` 或外部编辑器写同一 run；检测到预检后证据变化会中止，并刻意避开主线遇到普通异常时的样本清理分支。不能把该锁当作防止所有非协作外部写入的安全隔离。

## 验收覆盖与明确限制

- infra：执行/身份、清理状态、完整 JSONL 结构、请求/工具调用；full-wire 来自冻结契约，不依赖各 Harness 是否碰巧在 runtime 重复记录该字段。
- artifact：归档清单覆盖全部文件且大小/hash 一致；journal/失败尝试/最后成功代码-图片配对；渲染预算；有界 PNG 解码/非单色检查。验收不执行收集到的 `plot.py`。
- trajectory：ACTF 顺序与通用训练资格、ACTF→SFT 完全一致、主 agent 实际图像块中的参考图字节、冻结 prompt。显式复现历史 single-render 诊断时禁止产物读图；loop 要求成功产物的图片内容进入主轨迹，但不强制修改代码。
- 精确“参考图何时看到、每次渲染后何时看图”是 `process_audit`，未证实不冒充已证实。基本准入不要求旧 recorder 本就不存在的 marker；参考图若只见于子调用/参数/文件名仍然不通过内容证据检查。初始 user 图片可证明较早交付，同一步多个工具可能并发。
- 报告记录验收时间、主线/旁路 commit、实际验证器源码摘要、契约及证据指纹。源码摘要可区分未提交实现；安装包没有 Git 元数据时 commit 为 null，但摘要仍存在。
- 不评价图表与参考图的视觉相似度，不验证所有自然语言禁令；`visual_quality=not_evaluated`。PNG 沿用非交错 8-bit 格式限制，有限读取/解压；完整轨迹 JSON 单文件/行上限 128 MiB，哈希和 JSONL 逐块/逐行处理，不读全量数据集。

2026-09-25 用户决策及工程复核已纠正两项过度约束：

- **Q6 已关闭**：loop 修订可选，删除 pending_Q6 封锁。6 份 prompt 保留为 `historical_experiment`，用于按需复现，不把某次诊断的“一次渲染/禁止看图/900秒”变成永久默认。
- **Q7 纠偏**：旧 journal 明写关联 advisory；新验收不应把它升级为默认硬门槛。保持 recorder 输出不变，缺 marker 时仍可通过基础验收，但逐轮顺序报告未证实。未来若确实要研究严格循环，再独立增加显式实验机制，不阻挡基本合入。

当前验收版本为 v3，profile 为 v2。报告明确 `qualification_scope=artifact_and_trajectory_evidence`、`prompt_compliance=not_fully_audited`。基础准入不等于所有自然语言要求/逐轮顺序/视觉质量均达标。旧契约不静默改写，新启动应重新冻结配置。

## 可运行命令

全部任务合成回归（无 API、无 Docker，仅在临时目录执行自写的最小 Python/PNG fixture）：

```bash
PYTHON_BIN=/home/hongxin/miniconda3/envs/xtuner2/bin/python bash scripts/test_chart2code_regressions.sh
```

可设置 `LOG_DIR=/absolute/path` 保存新日志，或传 unittest 模块名只跑指定回归。测试包括采集后、resume 前、汇总前三处，替代旧测试直接调用主线任务钩子的方式。

只读 profile 预览：

```bash
PYTHON_BIN=/home/hongxin/miniconda3/envs/xtuner2/bin/python \
  PROFILE=loop-v3 bash scripts/describe_chart2code_profile.sh
```

对已由旁路入口生成 `chart2code/contracts.json` 的 run 离线重验（会写旁路报告，不写原样本）：

```bash
PYTHON_BIN=/home/hongxin/miniconda3/envs/xtuner2/bin/python \
  RUN_DIR=/absolute/path/to/run bash scripts/audit_chart2code_run.sh
```

正式导出需指定独立的新目录；不能使用 `REPORT_ONLY=1`：

```bash
PYTHON_BIN=/home/hongxin/miniconda3/envs/xtuner2/bin/python \
  RUN_DIR=/absolute/path/to/run MODE=export OUTPUT_DIR=/absolute/path/to/new-dataset \
  bash scripts/audit_chart2code_run.sh
```

报告位于 `<run>/chart2code/acceptance/` 和 `summary.json`；数据集有 `sft.jsonl` 与 `manifest.json`，图片按已选上游兼容 schema 内联，不依赖外部媒体文件。老 run 不应手填一个“PASS 契约”冒充新入口的运行冻结。

接下来：接入生产任务 CLI，固定真实服务与模型思考档位、旁路兼容开关、派生镜像和小样本 dry-run，再按必需组合做真实 canary。不再等待 Q6/Q7 才推进。当前没有真实模型调用、镜像构建或正式训练数据发布。
