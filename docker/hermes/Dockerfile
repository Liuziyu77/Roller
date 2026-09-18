ARG BASE_IMAGE=python:3.11-slim-bookworm
FROM ${BASE_IMAGE}

# The adapter relies on the request and persistence hooks at this revision.
ARG HERMES_REVISION=d15ed4445207dda418b984e8bda0f68f48b8c6f3
ARG UV_VERSION=0.11.6
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST
ARG PYPI_DOWNLOAD_BASE=https://files.pythonhosted.org
ARG UV_INSECURE_HOST
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
