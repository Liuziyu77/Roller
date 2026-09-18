# QwenClawBench evaluation report (2026-09-17, audited 2026-09-18)

This report records the full QwenClawBench v1.1 evaluation of
`bailian/deepseek-v4-flash` and `glm-5.2`, followed by targeted reruns after
the execution and grading issues found during the audit were fixed.

The benchmark contains 100 upstream tasks with three OpenClaw rollouts per
task, or 300 samples per model. The LLM judge was `glm-5.2`. Invalid samples
are counted as zero in every strict score below.

## Final audited results

| Model | Published 2026-09-17 score | Audited pre-fix baseline | Final strict score | Delta vs audited baseline | Valid samples | Auto >= 0.75 | Auto | LLM |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `bailian/deepseek-v4-flash` | 44.91 | 42.9735 | **46.5202** | **+3.5466** | 245/300 (81.67%) | 140 | 60.9912 | 55.8392 |
| `glm-5.2` | 39.43 | 39.5430 | **59.2645** | **+19.7214** | 282/300 (94.00%) | 186 | 72.4988 | 69.0950 |

The published and audited baselines differ because the original status-based
summary treated some samples as successful even though their final raw
assistant event was `aborted` after a provider idle timeout. The audited
baseline requires all of the following:

- sample status is `ok`;
- the grader has no error;
- the final assistant stop reason is neither `aborted` nor `error`;
- canonical ACTF and SFT exports exist.

The final result is a strict reconstruction by task id. It retains unaffected
samples and replaces only the samples selected for a fresh rerun. It is not a
second uniform execution of all 600 rollouts.

## What changed

The audit identified execution defects that were independent of model answer
quality:

1. OpenClaw's latest-visible reply reader could skip a final empty
   `aborted/error` event and return stale text from an earlier assistant turn.
2. The OpenClaw SDK applied an implicit 120-second stream-idle timeout.
3. Sixteen tasks used both `workspace/...` and workspace-relative paths, while
   their graders assumed both spellings referred to the same tree.
4. Dynamic OpenClaw tools could appear after the first provider request, but
   the wire trace retained only the first tool schema.
5. Inline image tool results could not be represented by the SFT exporter.
6. Generated `node_modules/` trees could inflate output snapshots or introduce
   ordinary package-manager symlinks that failed artifact validation.

The reruns used the current task bundle. OpenClaw request timeout was set to
300 seconds. GLM used `maxTokens=32768`, four workers, and one retry. DeepSeek
kept its provider token settings.

## Workspace-path rerun

Roller now merges an initial `workspace/` subtree into the actual workspace
root and creates `workspace -> .` before the agent starts. Prompts, assets, and
graders therefore address the same files through either spelling.

The 16 affected tasks were rerun three times per model:

| Model | Before replacement | After replacement | Overall delta | Auto delta |
| --- | ---: | ---: | ---: | ---: |
| `bailian/deepseek-v4-flash` | 42.9735 | 46.5202 | +3.5466 | +4.1926 |
| `glm-5.2` | 39.5430 | 44.1041 | +4.5611 | +4.8905 |

All 48 GLM samples were valid. DeepSeek produced 46 valid samples; one had no
visible reply and one could not export SFT because a completed `gateway` call
lacked its original schema.

## GLM provider-timeout rerun

After the workspace replacement, 95 of the GLM samples were still strict
failures caused by the old 120-second provider idle timeout. They were rerun
with the latest task bundle and fixed OpenClaw request settings.

| Metric | Before rerun | Rerun subset | Final 300 | Delta |
| --- | ---: | ---: | ---: | ---: |
| Valid samples | 205/300 | 77/95 | 282/300 | +77 |
| Overall | 44.1041 | 47.8748 | 59.2645 | +15.1604 |
| Auto | 52.7003 | 62.5217 | 72.4988 | +19.7985 |
| LLM | 50.2633 | 59.4684 | 69.0950 | +18.8317 |
| Auto >= 0.75 | 138 | 48 | 186 | +48 |

Of the 95 selected samples, 64 completed on the first attempt and 13 recovered
on the second attempt. The remaining 18 failed both attempts with
`LLM idle timeout (300s): no response from model`. There were no grader errors,
missing ids, duplicate ids, or missing artifacts among the 77 valid samples.

## Remaining strict failures

| Model | Provider idle timeout | Trajectory conversion | No visible reply | Total |
| --- | ---: | ---: | ---: | ---: |
| `bailian/deepseek-v4-flash` | 53 | 1 | 1 | 55 |
| `glm-5.2` | 18 | 0 | 0 | 18 |

These counts describe execution validity, not answer quality. Valid samples can
still score zero when their automated score is below the task's hard gate or
when required deliverables are incorrect.

## Official leaderboard reference

The published snapshot below is from the
[QwenClawBench leaderboard](https://skylenage-ai.github.io/QwenClawBench-Leaderboard/).

| Reference position | Model | Score | Auto | LLM |
| ---: | --- | ---: | ---: | ---: |
| 1 | Claude Sonnet 4.6 | 60.5 | 74.5 | 73.4 |
| 2 | Claude Opus 4.6 | 59.5 | 75.8 | 72.1 |
| reference projection 3 | **GLM-5.2 (this evaluation)** | **59.26** | **72.50** | **69.10** |
| 3 | GLM-5.1 | 58.7 | 74.6 | 70.9 |
| 4 | Qwen3.6-Plus | 57.4 | 74.7 | 69.0 |
| 5 | GPT-5.4high | 56.7 | 71.5 | 72.9 |
| 6 | MiMo-V2-Pro | 56.5 | 73.0 | 69.1 |
| 7 | Claude Opus 4.5high | 56.1 | 75.1 | 70.7 |
| 8 | GLM-5 | 54.9 | 72.9 | 66.7 |
| 9 | Qwen3.5-Plus | 53.0 | 71.0 | 65.8 |
| 10 | Kimi-K2.5 | 51.9 | 69.5 | 62.6 |
| 11 | DeepSeek-V3.2-Thinking | 50.7 | 71.2 | 62.4 |
| 12 | MiniMax-M2.7high | 50.5 | 70.6 | 65.1 |
| below published rows | **DeepSeek V4 Flash (this evaluation)** | **46.52** | **60.99** | **55.84** |

Numerically, GLM-5.2 is 0.56 points above the published GLM-5.1 row, 4.36
points above GLM-5, 0.24 below Claude Opus 4.6, and 1.24 below Claude Sonnet
4.6. DeepSeek V4 Flash is 4.18 points below the published
DeepSeek-V3.2-Thinking row.

These are reference deltas, not official ranks:

- the exact tested model versions are absent from the official leaderboard;
- the official leaderboard uses Claude as judge, while this evaluation uses
  GLM-5.2;
- the final local result selectively replaces audited failures and
  path-affected samples instead of uniformly rerunning every sample;
- harness, image, endpoint, task-bundle, and service-version differences can
  affect scores.

## Verification

The final GLM reconstruction contains exactly 95 expected rerun ids with no
missing or unexpected rows. All 77 valid replacements contain raw, ACTF, SFT,
and score artifacts. The final repository verification ran 340 unit tests
successfully with 18 environment-dependent skips; `compileall` and
`git diff --check` also passed.
