# OpenClaw、模型与 API 配置

本文说明 Roller 如何启动 OpenClaw、连接自定义模型服务，以及传递搜索与代理配置。Task 和文件声明分别见 [Task 格式参考](task-format.md) 与 [文件、Task Bundle 与结果存储](task-bundle-and-storage.md)。

## 1. 运行要求

采集前需要准备：

- Python 3.10+；
- 可用的 Docker daemon；
- 一个同时包含 `openclaw` 和 `python3` 的 Docker 镜像；
- 模型服务所需的 API 地址与凭证。

镜像可以通过参数或环境变量指定：

```bash
roller collect ... --docker-image your-openclaw-image:tag

# 或作为默认值
export ROLLER_OPENCLAW_IMAGE=your-openclaw-image:tag
```

如果 Docker daemon 在另一台机器上，可以使用 Docker CLI 自带的 `DOCKER_HOST` 等配置。Roller 调用的是本机 `docker` 命令，因此 `docker version` 能连接成功时，Roller 通常也能使用同一个 daemon。

```bash
docker version
docker image inspect your-openclaw-image:tag
```

## 2. 模型配置文件

`--model-config` 接受一份 OpenClaw models JSON。仓库中的文本模型模板是 [`examples/json/openclaw-models.json`](../examples/json/openclaw-models.json)：

```json
{
  "mode": "merge",
  "providers": {
    "my-openai-proxy": {
      "baseUrl": "${MY_PROXY_BASE_URL}",
      "apiKey": "${MY_PROXY_API_KEY}",
      "api": "openai-responses",
      "models": [
        {"id": "my-model", "name": "My model"}
      ]
    }
  }
}
```

运行时的 model ref 是 `provider/model`，对应上例中的：

```text
my-openai-proxy/my-model
```

Roller 在宿主机展开 `${ENV_NAME}`，把合并后的配置临时注入容器。引用的环境变量缺失或为空时，会在启动 rollout 前报错。临时文件使用后立即删除，最终 run 不保存展开后的 JSON。

### API base 与 adapter

`baseUrl` 用来设置自定义 API base，例如：

```text
https://api.example.com/v1
```

具体请求路径由 OpenClaw 根据 `api` adapter 生成：

| `api` | 典型后端 |
| --- | --- |
| `openai-responses` | 实现 OpenAI Responses API 的服务 |
| `openai-completions` | 只实现 Chat Completions 兼容接口的服务 |

如果服务端只提供 `/v1/chat/completions`，应选择 `openai-completions`，而不是把完整 endpoint 当成 `baseUrl`。模型 ID、adapter 和服务端能力必须一致；Roller 不会替后端转换不受支持的协议。

## 3. 环境变量与密钥

推荐从示例创建一个不会提交到 Git 的本地文件：

```bash
cp examples/env/api.env.example examples/env/api.env

# 编辑后加载到当前 shell
set -a
source examples/env/api.env
set +a
```

以下常用变量会在宿主机存在且非空时自动传入 OpenClaw 容器：

```text
OPENROUTER_API_KEY        OPENROUTER_BASE_URL
BRAVE_API_KEY             TAVILY_API_KEY
HTTP_PROXY                HTTPS_PROXY               NO_PROXY
http_proxy                https_proxy               no_proxy
```

还有两种传入方式：

- `runtime.env`：该 task 的硬依赖；变量缺失时直接失败；
- `--pass-env NAME`：为本次 run 增加一个可选透传变量。

例如，某个 task 必须使用内部服务：

```json
{
  "runtime": {
    "env": ["INTERNAL_SERVICE_TOKEN"]
  }
}
```

或者对所有 task 透传：

```bash
roller collect ... --pass-env INTERNAL_SERVICE_TOKEN
```

> [!IMPORTANT]
> Task 与模型 JSON 只写环境变量名或 `${ENV_NAME}` 占位符。不要把真实 API Key 写进仓库、task、命令行参数或 README。

为兼容 Interns2_data，宿主机的 `OPENROUTER_BASE_URL_OPENCLAW` 在没有设置 `OPENROUTER_BASE_URL` 时会映射为后者。新配置建议直接使用标准变量名。

## 4. 文本模型与多模态模型

普通文本模型使用：

```json
{"id":"my-model","name":"My model"}
```

视觉输入模型需要声明 image 能力：

```json
{
  "id": "my-multimodal-model",
  "name": "My Multimodal Model",
  "input": ["text", "image"]
}
```

完整模板见 [`examples/json/openclaw-models-multimodal.json`](../examples/json/openclaw-models-multimodal.json)。`input` 只是告诉 OpenClaw 如何使用模型；实际 API 后端仍必须支持对应 adapter 下的图像输入。

默认情况下，Roller 也会尝试把 task model 配置为 OpenClaw 的 image model。旧版 OpenClaw 不支持该设置时只记录 warning；可以用 `--no-set-image-model` 关闭。

## 5. Web Search

例如使用 Brave Search：

```bash
export BRAVE_API_KEY=your-key

roller collect \
  ... \
  --search-provider brave
```

`--search-provider` 配置 OpenClaw 的搜索 provider，API Key 仍通过环境变量传入。即使 task 不搜索，某些预构建镜像也可能在启动时校验已有的搜索配置；遇到 unresolved secret 一类错误时，应检查镜像中的 OpenClaw 配置或提供对应变量。

## 6. 并行、容器与端口

普通 rollout 使用一个独立容器；User Simulator rollout 同时使用任务 Agent 和 Simulator 两个容器。一个 session 会在多轮期间保持运行，不会每一轮重新创建容器。

OpenClaw gateway 端口按 worker slot 计算：

```text
gateway_port = --gateway-port + worker_slot × --gateway-port-step
```

默认起始端口是 `18789`，步长是 `10`。User Simulator 使用从 `--parallel` 开始的另一组 slot。因此 `--parallel 4` 的动态对话最多会同时占用 8 个容器和 8 个 gateway 端口。

使用默认 `--network host` 时，要确保端口范围在 Docker host 上空闲。并行 run 共用同一 Docker daemon 时，建议给它们设置不同的起始端口：

```bash
roller collect ... --gateway-port 22000 --gateway-port-step 10
```

## 7. 常用 OpenClaw 参数

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--docker-image` | `ROLLER_OPENCLAW_IMAGE` 或内置值 | OpenClaw 镜像 |
| `--docker-binary` | `docker` | Docker CLI 名称或路径 |
| `--model-config` | 无 | 自定义 provider/model JSON |
| `--network` | `host` | 容器网络模式 |
| `--gateway-port` | `18789` | gateway 起始端口 |
| `--gateway-port-step` | `10` | worker 之间的端口间隔 |
| `--gateway-startup-timeout` | `30` | 等待 gateway 就绪的秒数 |
| `--session-id` | `roller` | OpenClaw session ID 前缀 |
| `--thinking` | 无 | 全局 thinking 设置，可被 task 覆盖 |
| `--search-provider` | 无 | OpenClaw Web Search provider |
| `--pass-env NAME` | 无 | 增加一个环境变量名，可重复 |
| `--set-image-model` / `--no-set-image-model` | 开启 | 是否设置 OpenClaw image model |
| `--keep-container-on-error` | 关闭 | 出错后保留容器以便排查 |
| `--collect-path` | 无 | Legacy：额外回收容器绝对路径 |

全部参数以当前安装版本为准：

```bash
roller collect --help
```

## 8. 运行元数据与安全

Sample provenance 会记录 OpenClaw 版本、镜像及 digest、model ref、使用过的环境变量名称，以及脱敏后的 API base。API base 中的用户名、密码、query 和 fragment 不会进入结果；环境变量值和展开后的 API Key 也不会保存。

`--keep-container-on-error` 适合本地排错，但保留的容器可能含有临时配置、输入或运行环境变量。检查完成后应自行删除容器，不要在共享 Docker host 上长期保留。

## 9. 常见问题

### Docker preflight 失败

先运行 `docker version`。如果 Docker 使用远程 daemon，确认当前 shell 的 Docker context、`DOCKER_HOST` 和证书配置能够正常连接。

### `model config references missing environment variable`

模型 JSON 中的 `${ENV_NAME}` 没有在当前 shell 中设置，或值为空。加载本地 env 文件后重新运行。

### 模型请求返回 404 或协议错误

通常是 `baseUrl` 末尾路径或 `api` adapter 不匹配。确认服务端实现的是 Responses API 还是 Chat Completions API。

### gateway 端口冲突

调整 `--gateway-port`，并确保按照 `parallel × 参与者数量` 预留足够端口。

### 任务成功回复但 sample 失败

Roller 要求成功 sample 同时具有 OpenClaw raw trace 和可转换的 ACTF。还应检查输出目录里是否存在被拒绝的符号链接或特殊文件。具体规则见 [结果存储](task-bundle-and-storage.md#5-输出回收)。
