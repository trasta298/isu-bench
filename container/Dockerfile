FROM node:24-bookworm

ARG UID=1000
ARG GID=1000
ARG GO_VERSION=1.23.2
ARG CLAUDE_CODE_VERSION=2.1.321
ARG CODEX_VERSION=0.166.0
ARG DSH_VERSION=0.2.1-rc.5

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 netcat-openbsd openssh-client git curl jq \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" | tar -C /usr/local -xz
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
    "@deepseek-ai/dsh@${DSH_VERSION}"
RUN userdel -r node && groupadd -g "${GID}" agent && useradd -m -u "${UID}" -g "${GID}" -s /bin/bash agent

COPY bench /usr/local/bin/bench
ENV PATH=/usr/local/go/bin:/home/agent/go/bin:$PATH
USER agent
WORKDIR /work
