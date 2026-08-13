#!/bin/sh
set -eu

: "${CODEX_HOME:=/data/codex}"
export CODEX_HOME

mkdir -p "$CODEX_HOME"


setup_gitlab_auth() {
    # `codex login` may be run with the same image without GitLab variables.
    # In that case GitLab setup is intentionally skipped.
    if [ -z "${GITLAB_TOKEN:-}" ]; then
        return 0
    fi

    if [ -z "${GITLAB_URL:-}" ]; then
        echo "GITLAB_URL is required when GITLAB_TOKEN is set" >&2
        exit 1
    fi

    glab_bin="$(command -v glab || true)"
    if [ -z "$glab_bin" ] || [ ! -x "$glab_bin" ]; then
        echo "glab executable not found in PATH" >&2
        exit 1
    fi

    gitlab_host="$(python - "$GITLAB_URL" <<'PY'
import sys
from urllib.parse import urlparse

url = sys.argv[1]
parsed = urlparse(url if "://" in url else "https://" + url)

if not parsed.hostname:
    raise SystemExit("invalid GITLAB_URL: " + url)

if parsed.port is None:
    print(parsed.hostname)
else:
    print(f"{parsed.hostname}:{parsed.port}")
PY
)"

    # Authenticate glab using the single GitLab access token.
    # glab stores the correct GitLab username associated with the token
    # (including project access-token bot usernames).
    printf '%s' "$GITLAB_TOKEN" | "$glab_bin" auth login \
        --hostname "$gitlab_host" \
        --git-protocol https \
        --stdin \
        >/dev/null

    # Remove stale username overrides from older image versions.
    # In particular, forcing "oauth2" breaks glab's credential helper for
    # Project Access Tokens because their actual username is project_*_bot_*.
    git config --global --unset-all \
        "credential.https://${gitlab_host}.username" \
        >/dev/null 2>&1 || true

    # Clear a generic username override as well.
    git config --global --unset-all \
        "credential.username" \
        >/dev/null 2>&1 || true

    # Configure glab as the credential helper specifically for this GitLab host.
    # The empty helper resets inherited/global helpers for this URL before
    # glab is invoked.
    credential_key="credential.https://${gitlab_host}.helper"

    git config --global --unset-all "$credential_key" \
        >/dev/null 2>&1 || true

    git config --global --add "$credential_key" ""
    git config --global --add "$credential_key" \
        "!${glab_bin} auth git-credential"

    # Deterministic identity for autonomous commits unless a repository
    # overrides it locally.
    git config --global user.name \
        "${GIT_AUTHOR_NAME:-Codex Orchestrator}"

    git config --global user.email \
        "${GIT_AUTHOR_EMAIL:-codex-orchestrator@localhost}"

    # Verify glab authentication without printing the token.
    if ! "$glab_bin" auth status --hostname "$gitlab_host" >/dev/null 2>&1; then
        echo "GitLab authentication check failed for ${gitlab_host}" >&2
        exit 1
    fi
}


setup_gitlab_auth

exec /usr/bin/tini -- "$@"
