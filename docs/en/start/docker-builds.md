# Docker builds and package mirrors

The Codex, Claude Code, Hermes, OpenCode, DeepSeek, and Pi Dockerfiles accept the build arguments below. OpenClaw uses an upstream image; see the [OpenClaw guide](../harnesses/openclaw-configuration.md). Pass these settings with `docker build --build-arg`, not to `roller collect`.

## 1. Debian repositories

All six Dockerfiles support:

| Argument | Purpose | When unset or empty |
| --- | --- | --- |
| `DEBIAN_MIRROR` | Debian main repository URL | Keep the base image's main repository settings |
| `DEBIAN_SECURITY_MIRROR` | Debian security repository URL | Keep the base image's security repository settings |

The URLs are independent. Setting only the main mirror leaves the security mirror unchanged; the security URL need not be the main URL plus `-security`. This Pi example uses two different proxy paths. Replace the example addresses with working endpoints:

```bash
docker build \
  --build-arg DEBIAN_MIRROR=http://apt.example/repository/debian-proxy \
  --build-arg DEBIAN_SECURITY_MIRROR=http://apt.example/repository/debian-security-proxy \
  --build-arg NPM_REGISTRY=https://npm.example/repository/npm-proxy/ \
  -t roller-pi:0.85.1 docker/pi
```

The build checks `/etc/apt/sources.list` and `.list`/`.sources` files under `/etc/apt/sources.list.d/`. It replaces complete official repository URLs only: `deb.debian.org/debian` for main, and `deb.debian.org/debian-security` or `security.debian.org/debian-security` for security. HTTP, HTTPS, and trailing slashes are supported. Distribution suites, components, and signature settings remain unchanged.

Repositories already configured with other URLs in a custom base image, Ubuntu repositories, and third-party repositories remain unchanged. Missing source files are skipped. OpenCode, Pi, and DeepSeek still run apt only when their tool checks require it; Hermes, Codex, and Claude Code still run apt installation.

The Node slim bases used by Pi, Codex, and Claude Code may lack a system CA bundle. Before rewriting repositories or running apt, the build checks `/etc/ssl/certs/ca-certificates.crt`. If it is missing or empty, the build initializes it from [Node's bundled Mozilla root certificates](https://nodejs.org/docs/latest-v22.x/api/tls.html#tlsrootcertificates). This step needs no network and preserves an existing non-empty bundle. The later apt installation still includes Debian's `ca-certificates` package. HTTPS certificate verification and apt repository signature checks remain enabled.

For an internal HTTPS mirror using a private CA, install that CA into the base image's system trust store first. Node's bundled roots do not include private enterprise CAs.

## 2. npm registry

| Harness | Argument | Default behavior |
| --- | --- | --- |
| Codex, Claude Code, Pi | `NPM_REGISTRY` | When unset or empty, use the existing npm configuration, including the base image's `.npmrc` |
| OpenCode | `NPM_REGISTRY` | Defaults to `https://registry.npmjs.org` for binary package downloads and plugin dependency installation |

Codex, Claude Code, and Pi pass a non-empty argument through `npm_config_registry` during installation. It is not stored as a runtime `ENV` setting in the image. Version pins and existing integrity checks are unchanged.

An OpenCode mirror must serve npm package metadata and tarballs. Pi still downloads `fd` from GitHub Releases; changing the npm registry does not redirect that download.

## 3. Hermes Python dependencies

| Argument | Step controlled | Default |
| --- | --- | --- |
| `PIP_INDEX_URL` | pip installation of the pinned uv version | `https://pypi.org/simple` |
| `PIP_TRUSTED_HOST` | Host or host:port explicitly trusted by pip | Unset |
| `PYPI_DOWNLOAD_BASE` | Prefix of `files.pythonhosted.org` download URLs in `uv.lock` | `https://files.pythonhosted.org` |
| `UV_INSECURE_HOST` | Host where uv explicitly permits an insecure connection | Unset |

Prefer an HTTPS index with a valid certificate. By default, pip ignores plain HTTP indexes on non-loopback hosts, which can produce an error saying the pinned uv version was not found. For a trusted internal HTTP index, explicitly name its host:

```bash
docker build \
  --build-arg PIP_INDEX_URL=http://pypi.internal/simple \
  --build-arg PIP_TRUSTED_HOST=pypi.internal \
  -t roller-hermes:d15ed4445207 docker/hermes
```

`PIP_TRUSTED_HOST` affects pip only. If the uv download host also needs relaxed connection checks, set `UV_INSECURE_HOST` separately and name only the host you intend to trust. Neither option is enabled by default. See [pip trusted-host](https://pip.pypa.io/en/stable/cli/pip/#cmdoption-trusted-host) and [uv insecure host](https://docs.astral.sh/uv/reference/environment/#uv_insecure_host) for details.

`PIP_INDEX_URL` does not redirect Hermes' locked dependency downloads. `uv sync --frozen` still uses `uv.lock`; `PYPI_DOWNLOAD_BASE` changes only the download prefix. The mirror must preserve the `/packages/` path layout. Versions and SHA-256 checks remain unchanged. Hermes source is still fetched from GitHub.

## 4. Other downloads and proxies

These settings control apt, npm, or specific Python installation steps. They do not make the entire build work offline. Base images, GitHub downloads, and other dependencies still need network access:

- `BASE_IMAGE` selects a prepared base image for Hermes, OpenCode, DeepSeek, and Pi.
- Other build downloads can use Docker's `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` build arguments. Internal mirrors usually belong in `NO_PROXY`.
- The Docker daemon pulls base images and needs its own network configuration.

See [Docker build variables](https://docs.docker.com/build/building/variables/#proxy-arguments) for proxy settings.

## 5. Build configuration regression tests

Run the configuration tests from the repository root. CA bootstrap tests require Node, and npm configuration tests require npm:

```bash
python -m unittest discover -s tests -p test_docker_build_config.py -v
```

The real HTTPS test is skipped by default and requires Docker and network access to `deb.debian.org`. It uses the three Node Harnesses' default base images, executes the Dockerfile CA bootstrap and mirror configuration steps, downloads apt indexes over HTTPS, installs `ca-certificates`, and simulates installation of each Harness's apt packages. It does not install Harness npm dependencies or call a model API:

```bash
docker pull node:22.22.0-bookworm-slim
docker pull node:22-bookworm-slim
ROLLER_DOCKER_BUILD_NETWORK_TEST=1 python -m unittest discover \
  -s tests -p test_docker_build_https.py -v
```

The test forwards `http_proxy`, `https_proxy`, `no_proxy`, and their uppercase equivalents to the container. For a proxy on the host's loopback address on Linux, also set `ROLLER_DOCKER_TEST_NETWORK=host`. This selects the test container's network without changing Docker daemon configuration.
