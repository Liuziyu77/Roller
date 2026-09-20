# SWE-Bench Pro evaluation results

This report records the repaired and audited DeepSeek V4 Pro evaluation on the
fixed first 100 source indices of the SWE-Bench Pro public test split.

## Configuration

- Model: DeepSeek V4 Pro
- Harness: Roller with OpenClaw 2026.7.1
- Initial evaluated subset: source indices 0-70, Non-Think
- Recovery subset: unresolved initial tasks and source indices 71-99, High
- Execution: local Docker, host networking, parallelism 4
- Agent and grader budgets: 3600 seconds each per task
- Model request timeout: 900 seconds

The grader uses the official test patch, `run_script.sh`, parser, and
FAIL_TO_PASS/PASS_TO_PASS sets. A task resolves only when every required test is
reported as `PASSED`.

## Score progression

| Result set | Resolved | Rate | Interpretation |
| --- | ---: | ---: | --- |
| Initial Non-Think run | 32/71 | 45.07% | Original partial run |
| Same 71 patches after repaired grading | 33/71 | 46.48% | Regrade only; no new model attempt |
| Selective High attempts on previous failures | 6/38 | 15.79% | Difficult, failure-biased subset |
| Original 71 after regrade and selective High recovery | 39/71 | 54.93% | Cumulative recovery result |
| Originally pending 29 tasks, grader outcomes | 16/29 | 55.17% | Includes one conservative infrastructure-error zero |
| All valid High trajectories | 22/66 | 33.33% | Previous failures plus new tasks |
| Fixed first 100, grader outcomes | **55/100** | **55.00%** | Final conservative cumulative result |
| Fixed first 100, valid Harness completions | 55/99 | 55.56% | Excludes the one infrastructure error |

The repaired regrade recovered source index 59. Selective High attempts
recovered previous failures at indices 2, 15, 27, 28, 41, and 45.

## Official reference

| Published DeepSeek V4 Pro configuration | SWE-Bench Pro | Difference from 55.00% |
| --- | ---: | ---: |
| Non-Think | 52.1% | +2.90 points |
| High | 54.4% | +0.60 points |
| Max | 55.4% | -0.40 points |

References were checked on 2026-09-18:
[DeepSeek V4 Pro model card](https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro)
and the [Scale SWE-Bench Pro Public leaderboard](https://scale.com/leaderboard/swe_bench_pro_public).

These differences are reference projections, not claims of a better
independent pass@1 result. This run uses a sequential 100-task subset and
selectively regrades and retries earlier failures. The official public
leaderboard covers 731 tasks, and its harness and budget conditions differ.

## Infrastructure corrections

The core pass criterion remains unchanged. The recovery run adds these
infrastructure corrections:

- supply non-executable test fixtures omitted from an upstream test patch;
- exclude agent-authored test sources from the grading patch when they collide
  with hidden tests, while preserving the original `model.patch`;
- prepare missing Go module checksums within the original grading budget and
  retain the first failed test logs and dependency-only diff;
- preserve project runtimes while installing an isolated OpenClaw Node runtime;
- disable OpenClaw startup respawn for glibc Node on musl-based images;
- tolerate a model-client disconnect while the wire proxy finishes streaming.

## Completion and integrity

Source index 70 exhausted its one directed retry. Both collector attempts ended
without a visible terminal assistant response after a length stop. The official
grader still ran against the resulting empty patch and returned zero: 6 of 12
required tests passed. Therefore:

- the official grader executed for all 100 tasks;
- 99 tasks have a valid Harness completion;
- source index 70 remains an infrastructure error and is conservatively counted
  as zero in the 55/100 result.

The audit found no manifest hash mismatch, duplicate valid High result, score
recomputation mismatch, or SFT artifact issue. It verified 71 baseline
`score.json` files, 71 baseline model patches, and the baseline manifest.

Repository validation on the PR branch:

- 318 unit tests passed; 17 external-environment tests skipped;
- `compileall` passed;
- `git diff --check` passed;
- the built wheel contains the adapter, grader, and image modules;
- embedded grader SHA-256:
  `b1c859a985311e8e23e3f0046765274b2e70f247a41e67d27bae972bfe2db5ce`.
