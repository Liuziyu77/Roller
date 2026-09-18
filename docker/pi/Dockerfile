ARG BASE_IMAGE=node:22.22.0-bookworm-slim
FROM ${BASE_IMAGE}

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

RUN if ! (command -v python3 && command -v bash && command -v git && command -v rg \
          && command -v file && command -v unzip && command -v ps); then \
        apt-get update && apt-get install -y --no-install-recommends \
            python3 bash ca-certificates git ripgrep file unzip procps \
        && rm -rf /var/lib/apt/lists/*; \
    fi
# The SDK and agent/model libraries are pinned together. Node stays in the
# container; Roller itself remains dependency-free.
WORKDIR /opt/roller-pi
RUN if [ -n "${NPM_REGISTRY:-}" ]; then export npm_config_registry="$NPM_REGISTRY"; fi \
    && npm init -y \
    && npm install --omit=dev --ignore-scripts --save-exact \
        @earendil-works/pi-coding-agent@0.85.1 \
        @earendil-works/pi-agent-core@0.85.1 \
        @earendil-works/pi-ai@0.85.1 \
    && npm cache clean --force
ENV PI_OFFLINE=1 PI_TELEMETRY=0
# Older distributions ship fd without flags required by Pi's find tool.
# Install a pinned binary during build, keeping runtime tool downloads disabled.
COPY install-fd.py /tmp/install-pi-fd.py
RUN python3 /tmp/install-pi-fd.py && rm /tmp/install-pi-fd.py \
    && fd --max-results 1 --no-require-git --version
WORKDIR /roller/workspace
CMD ["bash"]
