# Chart2Code 旁路开发状态

2026-09-25 最小改动复核后，本分支仅保留原 Roller 的渲染记录器及代码—图片配对检查。**任务启动、产物回收和任务验收尚未端到端接通；不能宣称真实任务已跑通。**

## 当前保留

- `docker/chart2code/`：recorder 派生镜像、Python 包装器、PATH 配置及记录器。保留失败尝试、并发编号、解释器选择、信号传递等原有修复，不改写绘图程序的返回码或输出。
- `src/roller_chart2code/render_journal.py`：只读检查已存在的 journal、文件摘要、最后成功代码与图片。无 journal 返回 `None`；不要求所有任务产生 journal。工具调用关联沿原实现仅作辅助信息，缺 marker 不拒收。
- `tests/chart2code/test_render_recorder.py`：12 项本地合成回归及最小 PNG fixture。
- 通用 `src/roller/` 与主线候选保持一致，不引入任务判断、任务钩子或临时 prompt。

记录器及检查器是可复用的任务组件，并非完整的任务验收入口。当前 `roller collect` 不自动调用此检查器。

## 本轮撤回

撤回新建的固定历史 profiles/prompts、acceptance、PNG 非空解码器、pipeline、export、未接入的 visual_evidence、对应测试及两个 CLI。它们将局部实验或新增要求扩成了一套尚未接入采集入口的框架。

不再规定“一次渲染/禁止看图/必须修订/固定 900 秒”、固定 prompt 集、额外契约、全量清单、源码指纹或另一个发布协议。不是取消用户要求的任务验收，而是暂停这套过度设计；需要哪些最小任务检查，待复核现状后再定。

删除内容可从本分支撤回前提交 `1e363a6` 查看；原 `Roller` 中的历史实验文件未修改。没有删除运行结果、数据或镜像。

## 可运行验证

从本 worktree 执行：

```bash
PYTHON_BIN=/home/hongxin/miniconda3/envs/xtuner2/bin/python \
  bash scripts/test_chart2code_regressions.sh
```

可设置 `LOG_DIR=/absolute/path` 保存新日志，或传 unittest 模块名选择定向检查。无 API、无 Docker，仅在临时目录运行自写最小绘图 fixture，不执行历史采集到的代码。

本轮不启动生产 CLI 建设、镜像构建、真实模型调用或正式数据发布。后续仅在用户确认必要范围后继续接线；不以旧实施计划中的框架条目为默认要求。
