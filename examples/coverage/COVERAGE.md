# 覆盖清单

由 `generate.py` 生成。每个链接是一份恰好含 4 条任务的独立清单；运行说明见 [README](README.md)。

## 输入场景 × Harness

| 场景 | OpenClaw | Codex | Claude Code | Hermes | OpenCode | DeepSeek Harness | 验证点 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| formats/jsonl | [4 条](cases/formats/jsonl/openclaw.jsonl) | [4 条](cases/formats/jsonl/codex.jsonl) | [4 条](cases/formats/jsonl/claude-code.jsonl) | [4 条](cases/formats/jsonl/hermes.jsonl) | [4 条](cases/formats/jsonl/opencode.jsonl) | [4 条](cases/formats/jsonl/deepseek.jsonl) | Task 清单格式：jsonl |
| formats/json_array | [4 条](cases/formats/json_array/openclaw.json) | [4 条](cases/formats/json_array/codex.json) | [4 条](cases/formats/json_array/claude-code.json) | [4 条](cases/formats/json_array/hermes.json) | [4 条](cases/formats/json_array/opencode.json) | [4 条](cases/formats/json_array/deepseek.json) | Task 清单格式：json_array |
| formats/json_object | [4 条](cases/formats/json_object/openclaw.json) | [4 条](cases/formats/json_object/codex.json) | [4 条](cases/formats/json_object/claude-code.json) | [4 条](cases/formats/json_object/hermes.json) | [4 条](cases/formats/json_object/opencode.json) | [4 条](cases/formats/json_object/deepseek.json) | Task 清单格式：json_object |
| formats/jsonl_bom | [4 条](cases/formats/jsonl_bom/openclaw.jsonl) | [4 条](cases/formats/jsonl_bom/codex.jsonl) | [4 条](cases/formats/jsonl_bom/claude-code.jsonl) | [4 条](cases/formats/jsonl_bom/hermes.jsonl) | [4 条](cases/formats/jsonl_bom/opencode.jsonl) | [4 条](cases/formats/jsonl_bom/deepseek.jsonl) | Task 清单格式：jsonl_bom |
| dialogue/question | [4 条](cases/dialogue/question/openclaw.jsonl) | [4 条](cases/dialogue/question/codex.jsonl) | [4 条](cases/dialogue/question/claude-code.jsonl) | [4 条](cases/dialogue/question/hermes.jsonl) | [4 条](cases/dialogue/question/opencode.jsonl) | [4 条](cases/dialogue/question/deepseek.jsonl) | 对话输入：question；最多两轮 |
| dialogue/custom_keys | [4 条](cases/dialogue/custom_keys/openclaw.jsonl) | [4 条](cases/dialogue/custom_keys/codex.jsonl) | [4 条](cases/dialogue/custom_keys/claude-code.jsonl) | [4 条](cases/dialogue/custom_keys/hermes.jsonl) | [4 条](cases/dialogue/custom_keys/opencode.jsonl) | [4 条](cases/dialogue/custom_keys/deepseek.jsonl) | 对话输入：custom_keys；最多两轮 |
| dialogue/messages_strings | [4 条](cases/dialogue/messages_strings/openclaw.jsonl) | [4 条](cases/dialogue/messages_strings/codex.jsonl) | [4 条](cases/dialogue/messages_strings/claude-code.jsonl) | [4 条](cases/dialogue/messages_strings/hermes.jsonl) | [4 条](cases/dialogue/messages_strings/opencode.jsonl) | [4 条](cases/dialogue/messages_strings/deepseek.jsonl) | 对话输入：messages_strings；最多两轮 |
| dialogue/messages_objects | [4 条](cases/dialogue/messages_objects/openclaw.jsonl) | [4 条](cases/dialogue/messages_objects/codex.jsonl) | [4 条](cases/dialogue/messages_objects/claude-code.jsonl) | [4 条](cases/dialogue/messages_objects/hermes.jsonl) | [4 条](cases/dialogue/messages_objects/opencode.jsonl) | [4 条](cases/dialogue/messages_objects/deepseek.jsonl) | 对话输入：messages_objects；最多两轮 |
| dialogue/multiline | [4 条](cases/dialogue/multiline/openclaw.jsonl) | [4 条](cases/dialogue/multiline/codex.jsonl) | [4 条](cases/dialogue/multiline/claude-code.jsonl) | [4 条](cases/dialogue/multiline/hermes.jsonl) | [4 条](cases/dialogue/multiline/opencode.jsonl) | [4 条](cases/dialogue/multiline/deepseek.jsonl) | 对话输入：multiline；最多两轮 |
| routing/aliases | [4 条](cases/routing/aliases/openclaw.jsonl) | [4 条](cases/routing/aliases/codex.jsonl) | [4 条](cases/routing/aliases/claude-code.jsonl) | [4 条](cases/routing/aliases/hermes.jsonl) | [4 条](cases/routing/aliases/opencode.jsonl) | [4 条](cases/routing/aliases/deepseek.jsonl) | Harness / model / ID 声明：aliases |
| routing/model_cli | [4 条](cases/routing/model_cli/openclaw.jsonl) | [4 条](cases/routing/model_cli/codex.jsonl) | [4 条](cases/routing/model_cli/claude-code.jsonl) | [4 条](cases/routing/model_cli/hermes.jsonl) | [4 条](cases/routing/model_cli/opencode.jsonl) | [4 条](cases/routing/model_cli/deepseek.jsonl) | Harness / model / ID 声明：model_cli |
| routing/id_variants | [4 条](cases/routing/id_variants/openclaw.jsonl) | [4 条](cases/routing/id_variants/codex.jsonl) | [4 条](cases/routing/id_variants/claude-code.jsonl) | [4 条](cases/routing/id_variants/hermes.jsonl) | [4 条](cases/routing/id_variants/opencode.jsonl) | [4 条](cases/routing/id_variants/deepseek.jsonl) | Harness / model / ID 声明：id_variants |
| data/csv | [4 条](cases/data/csv/openclaw.jsonl) | [4 条](cases/data/csv/codex.jsonl) | [4 条](cases/data/csv/claude-code.jsonl) | [4 条](cases/data/csv/hermes.jsonl) | [4 条](cases/data/csv/opencode.jsonl) | [4 条](cases/data/csv/deepseek.jsonl) | 来自 Roller_Data 的小型 .csv 文件 |
| data/html | [4 条](cases/data/html/openclaw.jsonl) | [4 条](cases/data/html/codex.jsonl) | [4 条](cases/data/html/claude-code.jsonl) | [4 条](cases/data/html/hermes.jsonl) | [4 条](cases/data/html/opencode.jsonl) | [4 条](cases/data/html/deepseek.jsonl) | 来自 Roller_Data 的小型 .html 文件 |
| data/json | [4 条](cases/data/json/openclaw.jsonl) | [4 条](cases/data/json/codex.jsonl) | [4 条](cases/data/json/claude-code.jsonl) | [4 条](cases/data/json/hermes.jsonl) | [4 条](cases/data/json/opencode.jsonl) | [4 条](cases/data/json/deepseek.jsonl) | 来自 Roller_Data 的小型 .json 文件 |
| data/jsonl | [4 条](cases/data/jsonl/openclaw.jsonl) | [4 条](cases/data/jsonl/codex.jsonl) | [4 条](cases/data/jsonl/claude-code.jsonl) | [4 条](cases/data/jsonl/hermes.jsonl) | [4 条](cases/data/jsonl/opencode.jsonl) | [4 条](cases/data/jsonl/deepseek.jsonl) | 来自 Roller_Data 的小型 .jsonl 文件 |
| data/log | [4 条](cases/data/log/openclaw.jsonl) | [4 条](cases/data/log/codex.jsonl) | [4 条](cases/data/log/claude-code.jsonl) | [4 条](cases/data/log/hermes.jsonl) | [4 条](cases/data/log/opencode.jsonl) | [4 条](cases/data/log/deepseek.jsonl) | 来自 Roller_Data 的小型 .log 文件 |
| data/md | [4 条](cases/data/md/openclaw.jsonl) | [4 条](cases/data/md/codex.jsonl) | [4 条](cases/data/md/claude-code.jsonl) | [4 条](cases/data/md/hermes.jsonl) | [4 条](cases/data/md/opencode.jsonl) | [4 条](cases/data/md/deepseek.jsonl) | 来自 Roller_Data 的小型 .md 文件 |
| data/py | [4 条](cases/data/py/openclaw.jsonl) | [4 条](cases/data/py/codex.jsonl) | [4 条](cases/data/py/claude-code.jsonl) | [4 条](cases/data/py/hermes.jsonl) | [4 条](cases/data/py/opencode.jsonl) | [4 条](cases/data/py/deepseek.jsonl) | 来自 Roller_Data 的小型 .py 文件 |
| data/svg | [4 条](cases/data/svg/openclaw.jsonl) | [4 条](cases/data/svg/codex.jsonl) | [4 条](cases/data/svg/claude-code.jsonl) | [4 条](cases/data/svg/hermes.jsonl) | [4 条](cases/data/svg/opencode.jsonl) | [4 条](cases/data/svg/deepseek.jsonl) | 来自 Roller_Data 的小型 .svg 文件 |
| data/txt | [4 条](cases/data/txt/openclaw.jsonl) | [4 条](cases/data/txt/codex.jsonl) | [4 条](cases/data/txt/claude-code.jsonl) | [4 条](cases/data/txt/hermes.jsonl) | [4 条](cases/data/txt/opencode.jsonl) | [4 条](cases/data/txt/deepseek.jsonl) | 来自 Roller_Data 的小型 .txt 文件 |
| data/yaml | [4 条](cases/data/yaml/openclaw.jsonl) | [4 条](cases/data/yaml/codex.jsonl) | [4 条](cases/data/yaml/claude-code.jsonl) | [4 条](cases/data/yaml/hermes.jsonl) | [4 条](cases/data/yaml/opencode.jsonl) | [4 条](cases/data/yaml/deepseek.jsonl) | 来自 Roller_Data 的小型 .yaml 文件 |
| inputs/object | [4 条](cases/inputs/object/openclaw.jsonl) | [4 条](cases/inputs/object/codex.jsonl) | [4 条](cases/inputs/object/claude-code.jsonl) | [4 条](cases/inputs/object/hermes.jsonl) | [4 条](cases/inputs/object/opencode.jsonl) | [4 条](cases/inputs/object/deepseek.jsonl) | 文件包输入方式：object |
| inputs/sha256 | [4 条](cases/inputs/sha256/openclaw.jsonl) | [4 条](cases/inputs/sha256/codex.jsonl) | [4 条](cases/inputs/sha256/claude-code.jsonl) | [4 条](cases/inputs/sha256/hermes.jsonl) | [4 条](cases/inputs/sha256/opencode.jsonl) | [4 条](cases/inputs/sha256/deepseek.jsonl) | 文件包输入方式：sha256 |
| inputs/multiple | [4 条](cases/inputs/multiple/openclaw.jsonl) | [4 条](cases/inputs/multiple/codex.jsonl) | [4 条](cases/inputs/multiple/claude-code.jsonl) | [4 条](cases/inputs/multiple/hermes.jsonl) | [4 条](cases/inputs/multiple/opencode.jsonl) | [4 条](cases/inputs/multiple/deepseek.jsonl) | 文件包输入方式：multiple |
| inputs/directory | [4 条](cases/inputs/directory/openclaw.jsonl) | [4 条](cases/inputs/directory/codex.jsonl) | [4 条](cases/inputs/directory/claude-code.jsonl) | [4 条](cases/inputs/directory/hermes.jsonl) | [4 条](cases/inputs/directory/opencode.jsonl) | [4 条](cases/inputs/directory/deepseek.jsonl) | 文件包输入方式：directory |
| inputs/optional_missing | [4 条](cases/inputs/optional_missing/openclaw.jsonl) | [4 条](cases/inputs/optional_missing/codex.jsonl) | [4 条](cases/inputs/optional_missing/claude-code.jsonl) | [4 条](cases/inputs/optional_missing/hermes.jsonl) | [4 条](cases/inputs/optional_missing/opencode.jsonl) | [4 条](cases/inputs/optional_missing/deepseek.jsonl) | 文件包输入方式：optional_missing |
| inputs/unicode_paths | [4 条](cases/inputs/unicode_paths/openclaw.jsonl) | [4 条](cases/inputs/unicode_paths/codex.jsonl) | [4 条](cases/inputs/unicode_paths/claude-code.jsonl) | [4 条](cases/inputs/unicode_paths/hermes.jsonl) | [4 条](cases/inputs/unicode_paths/opencode.jsonl) | [4 条](cases/inputs/unicode_paths/deepseek.jsonl) | 文件包输入方式：unicode_paths |
| inputs/empty | [4 条](cases/inputs/empty/openclaw.jsonl) | [4 条](cases/inputs/empty/codex.jsonl) | [4 条](cases/inputs/empty/claude-code.jsonl) | [4 条](cases/inputs/empty/hermes.jsonl) | [4 条](cases/inputs/empty/opencode.jsonl) | [4 条](cases/inputs/empty/deepseek.jsonl) | 文件包输入方式：empty |
| inputs/default_root | [4 条](fixtures/openclaw-default-root.jsonl) | [4 条](fixtures/codex-default-root.jsonl) | [4 条](fixtures/claude-code-default-root.jsonl) | [4 条](fixtures/hermes-default-root.jsonl) | [4 条](fixtures/opencode-default-root.jsonl) | [4 条](fixtures/deepseek-default-root.jsonl) | 文件包输入方式：default_root |
| inputs/absolute_root | [4 条](cases/inputs/absolute_root/openclaw.jsonl) | [4 条](cases/inputs/absolute_root/codex.jsonl) | [4 条](cases/inputs/absolute_root/claude-code.jsonl) | [4 条](cases/inputs/absolute_root/hermes.jsonl) | [4 条](cases/inputs/absolute_root/opencode.jsonl) | [4 条](cases/inputs/absolute_root/deepseek.jsonl) | 文件包输入方式：absolute_root |
| inputs/legacy_workspace | [4 条](cases/inputs/legacy_workspace/openclaw.jsonl) | [4 条](cases/inputs/legacy_workspace/codex.jsonl) | [4 条](cases/inputs/legacy_workspace/claude-code.jsonl) | [4 条](cases/inputs/legacy_workspace/hermes.jsonl) | [4 条](cases/inputs/legacy_workspace/opencode.jsonl) | [4 条](cases/inputs/legacy_workspace/deepseek.jsonl) | 文件包输入方式：legacy_workspace |
| context/full | [4 条](cases/context/full/openclaw.jsonl) | [4 条](cases/context/full/codex.jsonl) | [4 条](cases/context/full/claude-code.jsonl) | [4 条](cases/context/full/hermes.jsonl) | [4 条](cases/context/full/opencode.jsonl) | [4 条](cases/context/full/deepseek.jsonl) | Context 注入 / capture：full |
| context/hash | [4 条](cases/context/hash/openclaw.jsonl) | [4 条](cases/context/hash/codex.jsonl) | [4 条](cases/context/hash/claude-code.jsonl) | [4 条](cases/context/hash/hermes.jsonl) | [4 条](cases/context/hash/opencode.jsonl) | [4 条](cases/context/hash/deepseek.jsonl) | Context 注入 / capture：hash |
| context/none | [4 条](cases/context/none/openclaw.jsonl) | [4 条](cases/context/none/codex.jsonl) | [4 条](cases/context/none/claude-code.jsonl) | [4 条](cases/context/none/hermes.jsonl) | [4 条](cases/context/none/opencode.jsonl) | [4 条](cases/context/none/deepseek.jsonl) | Context 注入 / capture：none |
| context/roles | [4 条](cases/context/roles/openclaw.jsonl) | [4 条](cases/context/roles/codex.jsonl) | [4 条](cases/context/roles/claude-code.jsonl) | [4 条](cases/context/roles/hermes.jsonl) | [4 条](cases/context/roles/opencode.jsonl) | [4 条](cases/context/roles/deepseek.jsonl) | Context 注入 / capture：roles |
| context/skill | [4 条](cases/context/skill/openclaw.jsonl) | [4 条](cases/context/skill/codex.jsonl) | [4 条](cases/context/skill/claude-code.jsonl) | [4 条](cases/context/skill/hermes.jsonl) | [4 条](cases/context/skill/opencode.jsonl) | [4 条](cases/context/skill/deepseek.jsonl) | Context 注入 / capture：skill |
| context/optional | [4 条](cases/context/optional/openclaw.jsonl) | [4 条](cases/context/optional/codex.jsonl) | [4 条](cases/context/optional/claude-code.jsonl) | [4 条](cases/context/optional/hermes.jsonl) | [4 条](cases/context/optional/opencode.jsonl) | [4 条](cases/context/optional/deepseek.jsonl) | Context 注入 / capture：optional |
| runtime/setup_string | [4 条](cases/runtime/setup_string/openclaw.jsonl) | [4 条](cases/runtime/setup_string/codex.jsonl) | [4 条](cases/runtime/setup_string/claude-code.jsonl) | [4 条](cases/runtime/setup_string/hermes.jsonl) | [4 条](cases/runtime/setup_string/opencode.jsonl) | [4 条](cases/runtime/setup_string/deepseek.jsonl) | 运行设置：setup_string |
| runtime/setup_list | [4 条](cases/runtime/setup_list/openclaw.jsonl) | [4 条](cases/runtime/setup_list/codex.jsonl) | [4 条](cases/runtime/setup_list/claude-code.jsonl) | [4 条](cases/runtime/setup_list/hermes.jsonl) | [4 条](cases/runtime/setup_list/opencode.jsonl) | [4 条](cases/runtime/setup_list/deepseek.jsonl) | 运行设置：setup_list |
| runtime/env | [4 条](cases/runtime/env/openclaw.jsonl) | [4 条](cases/runtime/env/codex.jsonl) | [4 条](cases/runtime/env/claude-code.jsonl) | [4 条](cases/runtime/env/hermes.jsonl) | [4 条](cases/runtime/env/opencode.jsonl) | [4 条](cases/runtime/env/deepseek.jsonl) | 运行设置：env |
| runtime/thinking | [4 条](cases/runtime/thinking/openclaw.jsonl) | [4 条](cases/runtime/thinking/codex.jsonl) | [4 条](cases/runtime/thinking/claude-code.jsonl) | [4 条](cases/runtime/thinking/hermes.jsonl) | [4 条](cases/runtime/thinking/opencode.jsonl) | [4 条](cases/runtime/thinking/deepseek.jsonl) | 运行设置：thinking |
| outputs/nested | [4 条](cases/outputs/nested/openclaw.jsonl) | [4 条](cases/outputs/nested/codex.jsonl) | [4 条](cases/outputs/nested/claude-code.jsonl) | [4 条](cases/outputs/nested/hermes.jsonl) | [4 条](cases/outputs/nested/opencode.jsonl) | [4 条](cases/outputs/nested/deepseek.jsonl) | 输出回收：nested |
| outputs/multiple | [4 条](cases/outputs/multiple/openclaw.jsonl) | [4 条](cases/outputs/multiple/codex.jsonl) | [4 条](cases/outputs/multiple/claude-code.jsonl) | [4 条](cases/outputs/multiple/hermes.jsonl) | [4 条](cases/outputs/multiple/opencode.jsonl) | [4 条](cases/outputs/multiple/deepseek.jsonl) | 输出回收：multiple |
| outputs/no_files | [4 条](cases/outputs/no_files/openclaw.jsonl) | [4 条](cases/outputs/no_files/codex.jsonl) | [4 条](cases/outputs/no_files/claude-code.jsonl) | [4 条](cases/outputs/no_files/hermes.jsonl) | [4 条](cases/outputs/no_files/opencode.jsonl) | [4 条](cases/outputs/no_files/deepseek.jsonl) | 输出回收：no_files |
| binary/png | [4 条](cases/binary/png/openclaw.jsonl) | [4 条](cases/binary/png/codex.jsonl) | [4 条](cases/binary/png/claude-code.jsonl) | [4 条](cases/binary/png/hermes.jsonl) | [4 条](cases/binary/png/opencode.jsonl) | [4 条](cases/binary/png/deepseek.jsonl) | 二进制 .png 传输和读取；不验证语义理解 |
| binary/pdf | [4 条](cases/binary/pdf/openclaw.jsonl) | [4 条](cases/binary/pdf/codex.jsonl) | [4 条](cases/binary/pdf/claude-code.jsonl) | [4 条](cases/binary/pdf/hermes.jsonl) | [4 条](cases/binary/pdf/opencode.jsonl) | [4 条](cases/binary/pdf/deepseek.jsonl) | 二进制 .pdf 传输和读取；不验证语义理解 |
| binary/wav | [4 条](cases/binary/wav/openclaw.jsonl) | [4 条](cases/binary/wav/codex.jsonl) | [4 条](cases/binary/wav/claude-code.jsonl) | [4 条](cases/binary/wav/hermes.jsonl) | [4 条](cases/binary/wav/opencode.jsonl) | [4 条](cases/binary/wav/deepseek.jsonl) | 二进制 .wav 传输和读取；不验证语义理解 |
| binary/zip | [4 条](cases/binary/zip/openclaw.jsonl) | [4 条](cases/binary/zip/codex.jsonl) | [4 条](cases/binary/zip/claude-code.jsonl) | [4 条](cases/binary/zip/hermes.jsonl) | [4 条](cases/binary/zip/opencode.jsonl) | [4 条](cases/binary/zip/deepseek.jsonl) | 二进制 .zip 传输和读取；不验证语义理解 |
| vision/png | [4 条](cases/vision/png/openclaw.jsonl) | [4 条](cases/vision/png/codex.jsonl) | [4 条](cases/vision/png/claude-code.jsonl) | [4 条](cases/vision/png/hermes.jsonl) | [4 条](cases/vision/png/opencode.jsonl) | [4 条](cases/vision/png/deepseek.jsonl) | 可选：真正调用图像查看工具识别纯色，需模型和工具支持视觉 |
| bench/file_grader | [4 条](cases/bench/file_grader/openclaw.jsonl) | [4 条](cases/bench/file_grader/codex.jsonl) | [4 条](cases/bench/file_grader/claude-code.jsonl) | [4 条](cases/bench/file_grader/hermes.jsonl) | [4 条](cases/bench/file_grader/opencode.jsonl) | [4 条](cases/bench/file_grader/deepseek.jsonl) | 容器销毁前用标准库评分；不调用 LLM judge |

## Task Agent × User Simulator

每个配对的 4 条任务依次验证：纯文本、CSV 两轮与文件产物、Memory 与自定义输出目录、max_turns 截断。

| Task Agent | User Simulator | 任务 |
| --- | --- | --- |
| openclaw | openclaw | [4 条](cases/simulators/pair/openclaw--openclaw.jsonl) |
| openclaw | codex | [4 条](cases/simulators/pair/openclaw--codex.jsonl) |
| openclaw | claude-code | [4 条](cases/simulators/pair/openclaw--claude-code.jsonl) |
| openclaw | hermes | [4 条](cases/simulators/pair/openclaw--hermes.jsonl) |
| openclaw | opencode | [4 条](cases/simulators/pair/openclaw--opencode.jsonl) |
| openclaw | deepseek | [4 条](cases/simulators/pair/openclaw--deepseek.jsonl) |
| codex | openclaw | [4 条](cases/simulators/pair/codex--openclaw.jsonl) |
| codex | codex | [4 条](cases/simulators/pair/codex--codex.jsonl) |
| codex | claude-code | [4 条](cases/simulators/pair/codex--claude-code.jsonl) |
| codex | hermes | [4 条](cases/simulators/pair/codex--hermes.jsonl) |
| codex | opencode | [4 条](cases/simulators/pair/codex--opencode.jsonl) |
| codex | deepseek | [4 条](cases/simulators/pair/codex--deepseek.jsonl) |
| claude-code | openclaw | [4 条](cases/simulators/pair/claude-code--openclaw.jsonl) |
| claude-code | codex | [4 条](cases/simulators/pair/claude-code--codex.jsonl) |
| claude-code | claude-code | [4 条](cases/simulators/pair/claude-code--claude-code.jsonl) |
| claude-code | hermes | [4 条](cases/simulators/pair/claude-code--hermes.jsonl) |
| claude-code | opencode | [4 条](cases/simulators/pair/claude-code--opencode.jsonl) |
| claude-code | deepseek | [4 条](cases/simulators/pair/claude-code--deepseek.jsonl) |
| hermes | openclaw | [4 条](cases/simulators/pair/hermes--openclaw.jsonl) |
| hermes | codex | [4 条](cases/simulators/pair/hermes--codex.jsonl) |
| hermes | claude-code | [4 条](cases/simulators/pair/hermes--claude-code.jsonl) |
| hermes | hermes | [4 条](cases/simulators/pair/hermes--hermes.jsonl) |
| hermes | opencode | [4 条](cases/simulators/pair/hermes--opencode.jsonl) |
| hermes | deepseek | [4 条](cases/simulators/pair/hermes--deepseek.jsonl) |
| opencode | openclaw | [4 条](cases/simulators/pair/opencode--openclaw.jsonl) |
| opencode | codex | [4 条](cases/simulators/pair/opencode--codex.jsonl) |
| opencode | claude-code | [4 条](cases/simulators/pair/opencode--claude-code.jsonl) |
| opencode | hermes | [4 条](cases/simulators/pair/opencode--hermes.jsonl) |
| opencode | opencode | [4 条](cases/simulators/pair/opencode--opencode.jsonl) |
| opencode | deepseek | [4 条](cases/simulators/pair/opencode--deepseek.jsonl) |
| deepseek | openclaw | [4 条](cases/simulators/pair/deepseek--openclaw.jsonl) |
| deepseek | codex | [4 条](cases/simulators/pair/deepseek--codex.jsonl) |
| deepseek | claude-code | [4 条](cases/simulators/pair/deepseek--claude-code.jsonl) |
| deepseek | hermes | [4 条](cases/simulators/pair/deepseek--hermes.jsonl) |
| deepseek | opencode | [4 条](cases/simulators/pair/deepseek--opencode.jsonl) |
| deepseek | deepseek | [4 条](cases/simulators/pair/deepseek--deepseek.jsonl) |

## 预期拒绝的输入

这些文件是带预期错误的测试描述，不能整份传给 `roller collect`；`suite.py validate` 会逐条隔离验证。

- [negative/dialogue](negative/dialogue.json)：4 条。
- [negative/paths](negative/paths.json)：4 条。
- [negative/integrity](negative/integrity.json)：4 条。
- [negative/collisions](negative/collisions.json)：4 条。
- [negative/configuration](negative/configuration.json)：4 条。
