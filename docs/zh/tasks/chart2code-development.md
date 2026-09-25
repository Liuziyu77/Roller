# Chart2Code 旁路开发状态

此分支继承通用候选提交 `341f359`，尚未完成任务流水线，不能当作真实采集已验收。

已迁入 `roller_chart2code.render_journal`、`roller_chart2code.visual_evidence` 与 `docker/chart2code/` recorder。通用 `roller` 包不导入这些任务模块。

运行已迁移 recorder 的合成回归：

```bash
PYTHON_BIN=/home/hongxin/miniconda3/envs/xtuner2/bin/python bash scripts/test_chart2code_regressions.sh
```

测试仅在临时目录执行自己生成的最小 Python/PNG fixture；不执行历史 run 的脚本，不请求模型，不构建 Docker 镜像。生产派生镜像仍待固定 base digest 和解释器路径。

原有 recorder 测试中两项直接依赖主线 Collector 任务钩子/验收器，未原样搬迁。这些要求没有取消：后续须在旁路 pipeline 的采集后、resume 前、汇总前三处补齐。当前脚本只覆盖已迁入模块，不意味着整个 C1–C6 通过。

待实现：冻结 profile/真实服务矩阵；适配上游兼容 SFT 的三门验收；参考图及产物图证据；旁路生命周期编排；合格数据集导出；受控 canary。所有任务报告应位于 `<run>/chart2code/acceptance/`，不依赖主线 sample 白名单保留。
