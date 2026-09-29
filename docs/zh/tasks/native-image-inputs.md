# 显式原生图片输入

图片输入需要在 `files.attach_images` 中按顺序列出已声明输入文件的目标路径。
默认空列表，不会因为 inputs 中存在 PNG 就自动发给模型。`files.input_mode`
仍仅表示上游原有的 `mount` 或 `copy`，与模型能否读取工具返回图片无关。

```json
{
  "id": "inspect-image",
  "prompt": "Describe the attached picture.",
  "files": {
    "inputs": [{"source": "picture.png", "target": "inputs/picture.png"}],
    "attach_images": ["inputs/picture.png"],
    "input_mode": "mount"
  }
}
```

Codex、Claude Code、OpenClaw、Pi、OpenCode、DeepSeek 使用各自的原生附件接口。
附件只在会话首轮发送；支持 reset 的 OpenClaw/OpenCode 会在新会话重新附图。
其他 Harness 不因此获得原先没有的 reset 能力。Hermes 的首轮附图尚未适配，
不要将此字段作为其已支持的能力。

Claude 优先读取 materialized bundle 中的不可变附件，而非重新打开原始来源。
选图配置写入 bundle digest，改变选图后不能沿用原来的 resume 身份。
OpenCode 显式附图要求 vision 开启；DeepSeek 的工具能力仍由显式 profile 决定，
首轮输入图片不自动改变权限或工具列表。

通用 SFT 沿用上游的 `<image>` 与顶层 `images` 对应格式，并扩展到用户图片。
无需替换文字时保留原生块顺序；单文本块可原位清洗。含 `image`、`input_image`
或 `image_url` 图片块的消息，若有零个或多个文本块、无法确定文字替换位置，
保留原始多模态块（包括包装文本），不丢图、不推断图片位置。纯文本及不含图片的
原有替换行为不变；不新增任务标记，也不修改 raw 或实际请求。
其他训练格式应使用独立命名的 exporter。
