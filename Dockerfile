FROM node:22-bookworm-slim

ARG CODEX_VERSION=latest
ARG GLAB_VERSION=1.109.0
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/opt/venv/bin:$PATH \
    HOME=/home/orchestrator \
    CODEX_HOME=/data/codex

# Docker Desktop / some VPS networks can stall on IPv6 Debian mirrors.
RUN printf '%s\n' \
      'Acquire::ForceIPv4 "true";' \
      'Acquire::Retries "5";' \
      'Acquire::http::Timeout "30";' \
      'Acquire::https::Timeout "30";' \
      > /etc/apt/apt.conf.d/99network \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      python3 \
      python3-venv \
      tini \
      bubblewrap \
    && rm -rf /var/lib/apt/lists/*

# GitLab CLI. TARGETARCH is provided by BuildKit (amd64 / arm64).
RUN case "${TARGETARCH:-amd64}" in \
      amd64|arm64) arch="${TARGETARCH:-amd64}" ;; \
      *) echo "Unsupported architecture for glab: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl --fail --show-error --location \
      "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_${arch}.deb" \
      -o /tmp/glab.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends /tmp/glab.deb \
    && rm -f /tmp/glab.deb \
    && rm -rf /var/lib/apt/lists/* \
    && glab version

RUN npm install -g "@openai/codex@${CODEX_VERSION}" \
    && rm -rf /root/.npm \
    && codex --version

RUN python3 -m venv /opt/venv

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY schema ./schema
COPY codex-config.toml /etc/codex/config.toml
COPY docker-entrypoint.sh /usr/local/bin/gitlab-codex-entrypoint

RUN useradd --create-home --uid 10001 orchestrator \
    && mkdir -p /data/codex /data/workspaces /data/results \
    && chown -R orchestrator:orchestrator /app /data /home/orchestrator \
    && chmod 0444 /etc/codex/config.toml \
    && chmod 0755 /usr/local/bin/gitlab-codex-entrypoint

USER orchestrator
EXPOSE 8080
VOLUME ["/data"]

ENTRYPOINT ["/usr/local/bin/gitlab-codex-entrypoint"]
CMD ["python", "-m", "app"]
