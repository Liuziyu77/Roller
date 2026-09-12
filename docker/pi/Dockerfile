ARG BASE_IMAGE=node:22.22.0-bookworm-slim
FROM ${BASE_IMAGE}

RUN if ! (command -v python3 && command -v bash && command -v git && command -v rg \
          && command -v file && command -v unzip && command -v ps); then \
        apt-get update && apt-get install -y --no-install-recommends \
            python3 bash ca-certificates git ripgrep file unzip procps \
        && rm -rf /var/lib/apt/lists/*; \
    fi
# The SDK and agent/model libraries are pinned together. Node stays in the
# container; Roller itself remains dependency-free.
WORKDIR /opt/roller-pi
RUN npm init -y \
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
