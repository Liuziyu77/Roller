ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

ARG OPENCODE_VERSION=1.18.30
ARG NPM_REGISTRY=https://registry.npmjs.org
ARG UV_VERSION=0.9.7
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
