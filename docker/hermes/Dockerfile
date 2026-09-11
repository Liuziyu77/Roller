ARG BASE_IMAGE=python:3.11-slim-bookworm
FROM ${BASE_IMAGE}

# The adapter relies on the request and persistence hooks at this revision.
ARG HERMES_REVISION=d15ed4445207dda418b984e8bda0f68f48b8c6f3
ARG UV_VERSION=0.11.6
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PYPI_DOWNLOAD_BASE=https://files.pythonhosted.org
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates git ripgrep file unzip procps \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir "uv==${UV_VERSION}"
RUN git init /opt/hermes && cd /opt/hermes \
    && git remote add origin https://github.com/NousResearch/hermes-agent.git \
    && git fetch --depth 1 origin "${HERMES_REVISION}" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${HERMES_REVISION}" \
    # Optional download mirror changes URLs only; uv still verifies lock hashes.
    && if [ "$PYPI_DOWNLOAD_BASE" != "https://files.pythonhosted.org" ]; then \
        sed -i "s|https://files.pythonhosted.org|${PYPI_DOWNLOAD_BASE%/}|g" uv.lock; \
       fi \
    && uv sync --frozen --no-dev --python "$(command -v python)" \
    && printf '%s\n' "${HERMES_REVISION}" > /opt/hermes/roller-revision

ENV PATH="/opt/hermes/.venv/bin:${PATH}" \
    PYTHONPATH=/opt/hermes \
    HERMES_HOME=/roller/hermes-home \
    HERMES_DISABLE_LAZY_INSTALLS=1 \
    TERMINAL_ENV=local \
    PYTHONUNBUFFERED=1
WORKDIR /roller/workspace
RUN python -c 'from run_agent import AIAgent; assert callable(AIAgent.run_conversation)'
CMD ["bash"]
