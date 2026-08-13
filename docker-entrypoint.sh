#!/bin/sh
set -eu

: "${CODEX_HOME:=/data/codex}"
export CODEX_HOME

mkdir -p "$CODEX_HOME"
config="$CODEX_HOME/config.toml"
touch "$config"

# Force file-backed ChatGPT credentials so login + token refresh live on the
# persistent /data volume instead of an ephemeral OS credential store.
if ! grep -Eq '^[[:space:]]*cli_auth_credentials_store[[:space:]]*=' "$config"; then
    printf '\ncli_auth_credentials_store = "file"\n' >> "$config"
fi

exec /usr/bin/tini -- "$@"
