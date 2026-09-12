ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ARG DEEPSEEK_VERSION=0.1.5rc1
RUN if ! (command -v bash && command -v git && command -v rg && command -v file && command -v unzip && command -v ps); then \
        apt-get update && apt-get install -y --no-install-recommends \
            bash ca-certificates git ripgrep file unzip procps \
        && rm -rf /var/lib/apt/lists/*; \
    fi
# The official platform wheel includes the native dsh runtime and its PTY.
# A dedicated environment avoids conflicts with tools installed in BASE_IMAGE.
RUN python3 -m venv /opt/roller-deepseek \
    && PIP_CONSTRAINT= /opt/roller-deepseek/bin/pip install --no-cache-dir \
        "deepseek-harness-sdk==${DEEPSEEK_VERSION}" \
        "deepseek-harness-runtime-bin==${DEEPSEEK_VERSION}"
ENV PATH="/opt/roller-deepseek/bin:${PATH}"
WORKDIR /roller/workspace
CMD ["bash"]
