#!/bin/sh
set -eu

: "${CODEX_HOME:=/data/codex}"
export CODEX_HOME
mkdir -p "$CODEX_HOME"

setup_gitlab_auth() {
    # The temporary `codex login` container is intentionally allowed to start
    # without GitLab variables. Normal orchestrator startup has them via .env.
    if [ -z "${GITLAB_TOKEN:-}" ]; then
        return 0
    fi

    if [ -z "${GITLAB_URL:-}" ]; then
        echo "GITLAB_URL is required when GITLAB_TOKEN is set" >&2
        exit 1
    fi

    gitlab_host="$(python - "$GITLAB_URL" <<'PY'
import sys
from urllib.parse import urlparse

url = sys.argv[1]
parsed = urlparse(url if '://' in url else 'https://' + url)
if not parsed.hostname:
    raise SystemExit('invalid GITLAB_URL: ' + url)
port = parsed.port
print(parsed.hostname if port is None else f'{parsed.hostname}:{port}')
PY
)"

    # Persist the access token in glab's config for the lifetime of the
    # container. GITLAB_TOKEN itself is deliberately not inherited by Codex.
    printf '%s' "$GITLAB_TOKEN" | glab auth login \
        --hostname "$gitlab_host" \
        --git-protocol https \
        --stdin >/dev/null

    # Ensure Git always supplies a non-empty HTTPS username. The access token is
    # returned by glab's credential helper; no token is written into the remote URL.
    git config --global "credential.https://${gitlab_host}.username" "oauth2"

    credential_key="credential.https://${gitlab_host}.helper"
    git config --global --unset-all "$credential_key" >/dev/null 2>&1 || true
    git config --global --add "$credential_key" ""
    git config --global --add "$credential_key" "!/usr/local/bin/glab auth git-credential"

    # Give autonomous commits deterministic identity unless the repository
    # overrides it locally.
    git config --global user.name "${GIT_AUTHOR_NAME:-Codex Orchestrator}"
    git config --global user.email "${GIT_AUTHOR_EMAIL:-codex-orchestrator@localhost}"

    # Do not print the token. This only verifies that glab can authenticate.
    glab auth status --hostname "$gitlab_host" >/dev/null
}

setup_gitlab_auth

exec /usr/bin/tini -- "$@"
