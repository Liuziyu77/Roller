FROM node:22-bookworm-slim

# Node slim has no system CA bundle. Seed it before contacting HTTPS apt mirrors;
# apt installs Debian's ca-certificates below. Preserve custom base-image roots.
RUN set -eu; \
    if [ ! -s /etc/ssl/certs/ca-certificates.crt ]; then \
        mkdir -p /etc/ssl/certs; \
        node -p 'require("node:tls").rootCertificates.join("\n")' \
            > /etc/ssl/certs/ca-certificates.crt; \
    fi

ARG CODEX_VERSION=0.149.0
ARG NPM_REGISTRY
ARG DEBIAN_MIRROR
ARG DEBIAN_SECURITY_MIRROR

# Match complete repository URLs so the main mirror cannot rewrite security URLs.
# Keep this block in each build context; the contexts are built independently.
RUN set -eu; \
    for source in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do \
        [ -f "$source" ] || continue; \
        if [ -n "${DEBIAN_MIRROR:-}" ]; then \
            mirror=$(printf '%s' "${DEBIAN_MIRROR%/}" | sed 's/[\\&@]/\\&/g'); \
            sed -i -E "s@https?://deb[.]debian[.]org/debian/?([[:space:]]|$)@$mirror\\1@g" "$source"; \
        fi; \
        if [ -n "${DEBIAN_SECURITY_MIRROR:-}" ]; then \
            mirror=$(printf '%s' "${DEBIAN_SECURITY_MIRROR%/}" | sed 's/[\\&@]/\\&/g'); \
            sed -i -E "s@https?://(deb|security)[.]debian[.]org/debian-security/?([[:space:]]|$)@$mirror\\2@g" "$source"; \
        fi; \
    done

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        file \
        git \
        jq \
        openssh-client \
        procps \
        python3 \
        python3-pip \
        python3-venv \
        ripgrep \
        unzip \
    && rm -rf /var/lib/apt/lists/* \
    && if [ -n "${NPM_REGISTRY:-}" ]; then export npm_config_registry="$NPM_REGISTRY"; fi \
    && npm install --global "@openai/codex@${CODEX_VERSION}" \
    && codex --version

# Codex puts its apply_patch alias on PATH at runtime, but Debian's /etc/profile
# resets PATH in the login shells Codex runs commands in. Keep a stable alias.
RUN set -eu; \
    codex_bin="$(find "$(npm root --global)/@openai/codex" -path '*/vendor/*/bin/codex' -type f | head -n 1)"; \
    test -n "$codex_bin"; \
    ln -s "$codex_bin" /usr/local/bin/apply_patch

ENV CODEX_CI=1 \
    DISABLE_AUTOUPDATER=1

WORKDIR /roller/workspace

CMD ["bash"]
