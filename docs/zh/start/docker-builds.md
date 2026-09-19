# Docker 构建与镜像源

Codex、Claude Code、Hermes、OpenCode、DeepSeek 和 Pi 的 Dockerfile 支持下面的构建参数。OpenClaw 使用上游镜像，准备方式见 [OpenClaw 指南](../harnesses/openclaw-configuration.md)。这些参数通过 `docker build --build-arg` 传入，不是 `roller collect` 参数。

## 1. Debian 软件源

六个 Dockerfile 均支持：

| 参数 | 用途 | 未设置或为空时 |
| --- | --- | --- |
| `DEBIAN_MIRROR` | Debian 主仓库地址 | 保留基础镜像的主源配置 |
| `DEBIAN_SECURITY_MIRROR` | Debian 安全仓库地址 | 保留基础镜像的安全源配置 |

两个地址独立生效。只设置主源不会修改安全源；安全源也不必是主源地址加 `-security`。下面以 Pi 和两条不同路径的内部代理为例，请换成可用地址：

```bash
docker build \
  --build-arg DEBIAN_MIRROR=http://apt.example/repository/debian-proxy \
  --build-arg DEBIAN_SECURITY_MIRROR=http://apt.example/repository/debian-security-proxy \
  --build-arg NPM_REGISTRY=https://npm.example/repository/npm-proxy/ \
  -t roller-pi:0.85.1 docker/pi
```

构建时会检查 `/etc/apt/sources.list` 和 `/etc/apt/sources.list.d/` 下的 `.list`、`.sources` 文件，只替换完整的官方仓库 URL：主源为 `deb.debian.org/debian`，安全源为 `deb.debian.org/debian-security` 或 `security.debian.org/debian-security`。支持 HTTP、HTTPS 和末尾斜杠。发行版代号、组件及签名配置不变。

自定义基础镜像中已经改成其他地址的仓库、Ubuntu 仓库和第三方仓库保持原样；这两个参数不会将它们转换成 Debian 仓库。没有对应源文件时会跳过替换。OpenCode、Pi、DeepSeek 原有的工具检查仍决定是否执行 apt；Hermes、Codex 和 Claude Code 仍执行 apt 安装。

Pi、Codex、Claude Code 使用的 Node slim 基础镜像可能没有系统 CA 证书包。构建会在替换软件源和首次执行 apt 之前，检查 `/etc/ssl/certs/ca-certificates.crt`；仅在文件缺失或为空时，用 [Node 内置的 Mozilla 根证书](https://nodejs.org/docs/latest-v22.x/api/tls.html#tlsrootcertificates) 初始化它。这个步骤不需要网络，已有的非空证书文件不会被覆盖，后续 apt 安装步骤仍包含 Debian 的 `ca-certificates` 包。HTTPS 证书验证和 apt 仓库签名校验保持开启。

内部 HTTPS 镜像如果使用私有 CA，需要预先将该 CA 安装到基础镜像的系统信任库中；Node 内置根证书不包含企业私有 CA。

## 2. npm registry

| Harness | 参数 | 默认行为 |
| --- | --- | --- |
| Codex、Claude Code、Pi | `NPM_REGISTRY` | 未设置或为空时沿用 npm 配置，包括基础镜像的 `.npmrc` |
| OpenCode | `NPM_REGISTRY` | 默认 `https://registry.npmjs.org`，用于二进制包下载和插件依赖安装 |

Codex、Claude Code 和 Pi 在安装步骤中将非空参数传给 `npm_config_registry`。参数不会写成镜像的运行时 `ENV`。版本固定和现有校验规则保持不变。

OpenCode 的下载镜像需要同时提供 npm 包元数据与 tarball。Pi 的 `fd` 仍从 GitHub Releases 下载；修改 npm registry 不会改变该地址。

## 3. Hermes 的 Python 依赖

| 参数 | 控制的步骤 | 默认值 |
| --- | --- | --- |
| `PIP_INDEX_URL` | pip 安装固定版本的 uv | `https://pypi.org/simple` |
| `PIP_TRUSTED_HOST` | pip 显式信任的主机或主机与端口 | 未设置 |
| `PYPI_DOWNLOAD_BASE` | `uv.lock` 中 `files.pythonhosted.org` 下载地址的前缀 | `https://files.pythonhosted.org` |
| `UV_INSECURE_HOST` | uv 显式允许不安全连接的主机 | 未设置 |

优先使用证书有效的 HTTPS 索引。pip 默认会忽略非回环主机上的纯 HTTP 索引，这可能表现为找不到固定版本的 uv。确需使用可信的内部 HTTP 索引时，显式设置其主机：

```bash
docker build \
  --build-arg PIP_INDEX_URL=http://pypi.internal/simple \
  --build-arg PIP_TRUSTED_HOST=pypi.internal \
  -t roller-hermes:d15ed4445207 docker/hermes
```

`PIP_TRUSTED_HOST` 只控制 pip，不会改变 uv 的校验。若 uv 下载端也需要显式放宽连接校验，单独设置 `UV_INSECURE_HOST`，仅填写要信任的主机。两者都不默认启用。具体规则见 [pip trusted-host](https://pip.pypa.io/en/stable/cli/pip/#cmdoption-trusted-host) 和 [uv insecure host](https://docs.astral.sh/uv/reference/environment/#uv_insecure_host)。

`PIP_INDEX_URL` 不会重定向已锁定的 Hermes 依赖下载。`uv sync --frozen` 仍使用 `uv.lock`；`PYPI_DOWNLOAD_BASE` 只替换下载前缀，镜像需保留 `/packages/` 路径布局，版本与 SHA-256 校验不变。Hermes 源码仍从 GitHub 拉取。

## 4. 其他下载与代理

这些参数分别控制 apt、npm 或指定的 Python 下载步骤，不能让整个构建自动离线。基础镜像拉取、GitHub 下载和其他依赖仍需要网络：

- `BASE_IMAGE` 可为 Hermes、OpenCode、DeepSeek、Pi 选择已准备好的基础镜像。
- 构建中其他下载可使用 Docker 的 `HTTP_PROXY`、`HTTPS_PROXY` 和 `NO_PROXY` 构建参数。内部镜像通常需要加入 `NO_PROXY`。
- 基础镜像的拉取由 Docker daemon 处理，需要在 daemon 侧配置相应网络。

代理参数用法见 [Docker 构建变量](https://docs.docker.com/build/building/variables/#proxy-arguments)。

## 5. 构建配置回归测试

在仓库根目录执行配置测试；CA 初始化测试需要 Node，npm 配置测试需要 npm：

```bash
python -m unittest discover -s tests -p test_docker_build_config.py -v
```

真实 HTTPS 测试默认跳过，需要 Docker 和可访问 `deb.debian.org` 的网络。它使用三个 Node Harness 的默认基础镜像，执行 Dockerfile 中的证书初始化与镜像替换步骤，通过 HTTPS 下载 apt 索引、实际安装 `ca-certificates`，再模拟安装各 Harness 所需的 apt 包，不安装 Harness 的 npm 依赖，也不调用模型 API：

```bash
docker pull node:22.22.0-bookworm-slim
docker pull node:22-bookworm-slim
ROLLER_DOCKER_BUILD_NETWORK_TEST=1 python -m unittest discover \
  -s tests -p test_docker_build_https.py -v
```

测试会向容器传递 `http_proxy`、`https_proxy`、`no_proxy` 及其大写形式。Linux 上使用宿主机回环地址的代理时，可额外设置 `ROLLER_DOCKER_TEST_NETWORK=host`；这只影响测试容器的网络，不修改 Docker daemon 配置。
