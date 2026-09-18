ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

ARG OPENCODE_VERSION=1.18.30
ARG NPM_REGISTRY=https://registry.npmjs.org
ARG UV_VERSION=0.9.7
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

RUN if ! (command -v bash && command -v git && command -v rg \
          && command -v file && command -v unzip && command -v ps); then \
        apt-get update && apt-get install -y --no-install-recommends \
            bash ca-certificates git ripgrep file unzip procps \
        && rm -rf /var/lib/apt/lists/*; \
    fi
RUN python3 -m pip install --no-cache-dir "uv==${UV_VERSION}"
COPY install.py /tmp/install-opencode.py
RUN python3 /tmp/install-opencode.py "${OPENCODE_VERSION}" "${NPM_REGISTRY}" \
    && rm /tmp/install-opencode.py \
    && test "$(opencode --version)" = "${OPENCODE_VERSION}"

ENV OPENCODE_DISABLE_AUTOUPDATE=1 OPENCODE_DISABLE_MODELS_FETCH=1
COPY prepare.py /tmp/prepare-opencode.py
RUN python3 /tmp/prepare-opencode.py "${OPENCODE_VERSION}" "${NPM_REGISTRY}" \
    && rm /tmp/prepare-opencode.py
WORKDIR /roller/workspace
CMD ["bash"]
