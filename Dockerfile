FROM node:22-bookworm-slim

ARG CODEX_VERSION=latest

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/opt/venv/bin:$PATH \
    HOME=/home/orchestrator \
    CODEX_HOME=/data/codex

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git python3 python3-venv tini bubblewrap \
    && npm install -g "@openai/codex@${CODEX_VERSION}" \
    && python3 -m venv /opt/venv \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /root/.npm

RUN mkdir -p /etc/codex
COPY codex-config.toml /etc/codex/config.toml
RUN chmod 0444 /etc/codex/config.toml
ENV CODEX_HOME=/data/codex

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY schema ./schema
COPY docker-entrypoint.sh /usr/local/bin/gitlab-codex-entrypoint

RUN useradd --create-home --uid 10001 orchestrator \
    && mkdir -p /data/codex /data/workspaces /data/results \
    && chown -R orchestrator:orchestrator /app /data /home/orchestrator \
    && chmod 0755 /usr/local/bin/gitlab-codex-entrypoint

USER orchestrator
EXPOSE 8080
VOLUME ["/data"]

ENTRYPOINT ["/usr/local/bin/gitlab-codex-entrypoint"]
CMD ["python", "-m", "app"]
