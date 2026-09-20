# SWE-Bench Pro 评测结果

本文记录 DeepSeek V4 Pro 在 SWE-Bench Pro 公共测试集固定前 100 个源索引上的修复、重评与审计结果。

## 评测配置

- 模型：DeepSeek V4 Pro
- Harness：Roller + OpenClaw 2026.7.1
- 初始子集：源索引 0-70，Non-Think
- 恢复子集：初始未解决任务及源索引 71-99，High
- 执行环境：本地 Docker、host 网络、并发 4
- 每题 Agent 与 grader 预算：各 3600 秒
- 模型请求超时：900 秒

grader 使用官方 test patch、`run_script.sh`、parser 及
FAIL_TO_PASS/PASS_TO_PASS 集合；只有全部必要测试均为 `PASSED` 才算解决。

## 得分进展

| 结果集合 | 解决数 | 通过率 | 解释 |
| --- | ---: | ---: | --- |
| 初始 Non-Think | 32/71 | 45.07% | 原始部分运行 |
| 修复评分后重评相同 71 份补丁 | 33/71 | 46.48% | 仅重评，没有新模型尝试 |
| 对原失败题选择性 High 尝试 | 6/38 | 15.79% | 偏向困难失败题的子集 |
| 原 71 题经重评和选择性 High 恢复后 | 39/71 | 54.93% | 累计恢复结果 |
| 原待评 29 题的 grader 结果 | 16/29 | 55.17% | 包含一个基础设施错误保守记零 |
| 所有有效 High 轨迹 | 22/66 | 33.33% | 原失败题与新增任务合计 |
| 固定前 100 题 grader 结果 | **55/100** | **55.00%** | 最终保守累计结果 |
| 固定前 100 题有效 Harness 完成态 | 55/99 | 55.56% | 排除一个基础设施错误 |

修复后重评恢复了源索引 59；选择性 High 尝试恢复了原失败题
2、15、27、28、41 和 45。

## 官方参考

| DeepSeek V4 Pro 公开配置 | SWE-Bench Pro | 与 55.00% 的差值 |
| --- | ---: | ---: |
| Non-Think | 52.1% | +2.90 个百分点 |
| High | 54.4% | +0.60 个百分点 |
| Max | 55.4% | -0.40 个百分点 |

参考数据核对于 2026-09-18：
[DeepSeek V4 Pro 模型卡](https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro)
及 [Scale SWE-Bench Pro Public 榜单](https://scale.com/leaderboard/swe_bench_pro_public)。

这些差值仅为参考投影，不代表优于官方独立 pass@1。本次使用顺序选择的 100
题，并对早期失败进行了选择性重评和重试；官方 Public 榜单覆盖 731 题，harness
和预算条件也不同。

## 基础设施修复

核心通过标准保持不变，恢复运行加入以下基础设施修复：

- 补齐上游 test patch 遗漏的非执行测试 fixture；
- 模型自建测试与隐藏测试冲突时，从评分补丁排除模型测试源码，同时保留原始
  `model.patch`；
- 在原评分预算内补齐缺失的 Go module checksum，并保存首次失败日志及仅依赖差异；
- 保留项目运行时，为 OpenClaw 安装隔离 Node；
- musl 镜像运行 glibc Node 时关闭 OpenClaw 启动 respawn；
- wire proxy 向已断开的模型客户端写流时安全结束。

## 完成度与完整性

源索引 70 用完了唯一一次定向重试。collector 内两次尝试均在 length stop
后没有形成可见的最终 assistant 回复。官方 grader 仍对产生的空补丁完成测试并判
0 分：12 个必要测试中通过 6 个。因此：

- 固定 100 题全部执行了官方 grader；
- 99 题具有有效 Harness 完成态；
- 源索引 70 保留为基础设施错误，并在 55/100 中保守记零。

审计未发现 manifest 哈希不一致、重复有效 High 结果、分数重算不一致或 SFT
产物问题；共核验 71 份基线 `score.json`、71 份基线模型补丁及基线 manifest。

PR 分支仓库验证：

- 318 项单元测试通过，17 项外部环境测试跳过；
- `compileall` 通过；
- `git diff --check` 通过；
- 构建的 wheel 包含 adapter、grader 和镜像模块；
- 内嵌 grader SHA-256：
  `b1c859a985311e8e23e3f0046765274b2e70f247a41e67d27bae972bfe2db5ce`。
