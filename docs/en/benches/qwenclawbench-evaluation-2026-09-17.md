# QwenClawBench evaluation report (2026-09-17)

This report records a full QwenClawBench v1.1 evaluation of
`bailian/deepseek-v4-flash` and `glm-5.2`. It covers all 100 upstream tasks
with three OpenClaw rollouts per task, for 300 samples per model and 600
samples in total. The LLM judge was `glm-5.2`.

GLM errors, grader errors, and timeouts were retried once. DeepSeek failures
were not retried. In the comparison score, samples that still failed are
counted as zero.

## Final results

| Model | Original score (failures=0) | Final grader score | Final comparison score (failures=0) | Retry delta | Original anomalies | Retried | Recovered | Still failed | Reference position* |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `bailian/deepseek-v4-flash` | 44.91 | 46.91 | 44.91 | +0.00 | 15 | 0 | 0 | 15 | 13 |
| `glm-5.2` | 37.37 | 40.43 | 39.43 | +2.06 | 58 | 58 | 29 | 29 | 13 |

*Reference position is hypothetical against the published rows and is not an
official rank.

## GLM retry outcome

Of the 58 selected GLM samples, 29 recovered and 29 still failed. The recovery
rate was 50.00%, and the strict score increased by 2.06 points.

The remaining failures break down as follows:

| Failure type | Count |
| --- | ---: |
| No visible assistant response | 14 |
| Trajectory conversion | 11 |
| Unsafe output rejected | 3 |
| Timeout | 1 |

### Remaining GLM failures

| Task | Status | Type | Reason |
| --- | --- | --- | --- |
| `task_00001_moltbook_social_media_explorer_skill__run_01` | error | trajectory_conversion | `steps[0]` contains an empty assistant message |
| `task_00015_persona_config_rewriter_skill__run_01` | error | trajectory_conversion | `steps[0]` contains an empty assistant message |
| `task_00021_generate_canonical_dsl_v1_1_scripts_for_scratch_survival_game__run_03` | error | model_no_visible_response | OpenClaw turn produced no visible assistant response |
| `task_00033_ai_browser_project_git_initialization_and_workspace_setup__run_03` | error | trajectory_conversion | `steps[0]` contains an empty assistant message |
| `task_00037_debug_npm_start_failure_in_express_api_server__run_01` | error | unsafe_output_rejected | Unsafe output files were rejected |
| `task_00037_debug_npm_start_failure_in_express_api_server__run_02` | error | unsafe_output_rejected | Unsafe output files were rejected |
| `task_00037_debug_npm_start_failure_in_express_api_server__run_03` | timeout | timeout | Task exceeded 1800 seconds |
| `task_00038_plan_project_folder_structure_for_new_blue_ocean_project__run_01` | error | model_no_visible_response | OpenClaw turn produced no visible assistant response |
| `task_00038_plan_project_folder_structure_for_new_blue_ocean_project__run_03` | error | model_no_visible_response | OpenClaw turn produced no visible assistant response |
| `task_00040_server_status_report_compilation__run_01` | error | trajectory_conversion | `steps[2]` contains an empty assistant message |
| `task_00040_server_status_report_compilation__run_03` | error | model_no_visible_response | OpenClaw turn produced no visible assistant response |
| `task_00042_doraemon_character_drawing_script_documentation__run_01` | error | model_no_visible_response | OpenClaw turn produced no visible assistant response |
| `task_00042_doraemon_character_drawing_script_documentation__run_02` | error | model_no_visible_response | OpenClaw turn produced no visible assistant response |
| `task_00042_doraemon_character_drawing_script_documentation__run_03` | error | trajectory_conversion | `steps[15].observation[0].content[1]` is non-text; text-only SFT export would lose data |
| `task_00044_memory_retrieval_system_seed_eviction_diagnosis_and_fix_proposal__run_01` | error | model_no_visible_response | OpenClaw turn produced no visible assistant response |
| `task_00045_draft_technical_proposal_section_8_5_top_lifting_points_for_anechoic__run_02` | error | model_no_visible_response | OpenClaw turn produced no visible assistant response |
| `task_00050_design_memory_architecture_for_personal_ai_assistant__run_03` | error | model_no_visible_response | OpenClaw turn produced no visible assistant response |
| `task_00053_extract_keywords_and_classify_intent_from_user_query__run_01` | error | trajectory_conversion | `steps[0]` contains an empty assistant message |
| `task_00062_llm_benchmark_design_statistical_analysis_and_execution_planning__run_02` | error | model_no_visible_response | OpenClaw turn produced no visible assistant response |
| `task_00072_spark_fi_liquidity_monitor_delta_analysis_and_alert_report__run_02` | error | trajectory_conversion | `steps[17]` contains an empty assistant message |
| `task_00072_spark_fi_liquidity_monitor_delta_analysis_and_alert_report__run_03` | error | unsafe_output_rejected | Unsafe output files were rejected |
| `task_00073_2026_new_issuance_p_l_decomposition_and_year_over_year_analysis__run_01` | error | trajectory_conversion | `steps[3]` contains an empty assistant message |
| `task_00073_2026_new_issuance_p_l_decomposition_and_year_over_year_analysis__run_03` | error | trajectory_conversion | `steps[3]` contains an empty assistant message |
| `task_00075_sector_momentum_rotation_backtest_with_data_quality_traps__run_01` | error | trajectory_conversion | `steps[1]` contains an empty assistant message |
| `task_00075_sector_momentum_rotation_backtest_with_data_quality_traps__run_02` | error | model_no_visible_response | OpenClaw turn produced no visible assistant response |
| `task_00079_trade_thesis_assessment_for_la_usdt_long_position__run_01` | error | model_no_visible_response | OpenClaw turn produced no visible assistant response |
| `task_00079_trade_thesis_assessment_for_la_usdt_long_position__run_02` | error | model_no_visible_response | OpenClaw turn produced no visible assistant response |
| `task_00085_thesis_project_day_of_schedule_and_priority_plan__run_02` | error | model_no_visible_response | OpenClaw turn produced no visible assistant response |
| `task_00086_command_prefix_security_analysis__run_02` | error | trajectory_conversion | `steps[3]` contains an empty assistant message |

## DeepSeek failures

DeepSeek had 15 failures: 12 trajectory conversion errors, two unsafe-output
rejections, and one timeout. Per the evaluation policy, these samples were not
retried.

| Task | Status | Type | Reason |
| --- | --- | --- | --- |
| `task_00010_openclaw_subtask_isolation_configuration_skill__run_03` | error | trajectory_conversion | Tool result has no matching call: `call_68fd749b807045ef96b4ad5b` |
| `task_00014_blockbeats_monitor_skill_creation_with_state_recovery__run_01` | error | unsafe_output_rejected | Unsafe output files were rejected |
| `task_00014_blockbeats_monitor_skill_creation_with_state_recovery__run_03` | error | trajectory_conversion | `steps[7]` contains an empty assistant message |
| `task_00021_generate_canonical_dsl_v1_1_scripts_for_scratch_survival_game__run_02` | error | trajectory_conversion | `steps[0]` contains an empty assistant message |
| `task_00037_debug_npm_start_failure_in_express_api_server__run_01` | error | unsafe_output_rejected | Unsafe output files were rejected |
| `task_00037_debug_npm_start_failure_in_express_api_server__run_03` | timeout | timeout | Task exceeded 1800 seconds |
| `task_00042_doraemon_character_drawing_script_documentation__run_01` | error | trajectory_conversion | `steps[15].observation[0].content[1]` is non-text; text-only SFT export would lose data |
| `task_00042_doraemon_character_drawing_script_documentation__run_02` | error | trajectory_conversion | `steps[19]` contains an empty assistant message |
| `task_00057_competitor_website_change_analysis_targetimpact_cta_shift__run_01` | error | trajectory_conversion | `steps[0]` contains an empty assistant message |
| `task_00058_did_regression_on_simulated_panel_data__run_01` | error | trajectory_conversion | `steps[33]` contains an empty assistant message |
| `task_00061_rag_traffic_forecast_statistical_assessment__run_03` | error | trajectory_conversion | `steps[0]` contains an empty assistant message |
| `task_00069_polymarket_btc_15min_monitor_skill_creation__run_03` | error | trajectory_conversion | `steps[0]` contains an empty assistant message |
| `task_00072_spark_fi_liquidity_monitor_delta_analysis_and_alert_report__run_02` | error | trajectory_conversion | `steps[44]` contains an empty assistant message |
| `task_00074_pol_usdc_arbitrage_monitor_execution__run_01` | error | trajectory_conversion | Tool result has no matching call: `call_7e38135836d644118b50458c` |
| `task_00076_build_a_share_paper_trading_skill_with_strategy_engine_and_backtesting__run_02` | error | trajectory_conversion | Tool result has no matching call: `call_30f6a4a13eea4ded964e32c6` |

## Official leaderboard reference

The published snapshot below is from the
[QwenClawBench leaderboard](https://skylenage-ai.github.io/QwenClawBench-Leaderboard/).

| Rank | Official model | Score | Auto | LLM |
| ---: | --- | ---: | ---: | ---: |
| 1 | Claude Sonnet 4.6 | 60.5 | 74.5 | 73.4 |
| 2 | Claude Opus 4.6 | 59.5 | 75.8 | 72.1 |
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

## Comparison

- GLM-5.2 scored 39.43 with failures counted as zero. This is 19.27 points
  below the published GLM-5.1 score and 15.47 points below GLM-5.
- DeepSeek V4 Flash scored 44.91 with failures counted as zero. This is 5.79
  points below the published DeepSeek-V3.2-Thinking score.

These differences are reference deltas, not direct model regressions or
official ranks:

- The exact tested model versions are absent from the official leaderboard.
- The official leaderboard uses Claude as judge; this evaluation uses GLM-5.2.
- GLM failures and timeouts were selectively retried once; DeepSeek failures
  and timeouts were not retried.
- Harness, image, endpoint, and service-version differences can affect scores.
