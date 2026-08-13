#!/bin/sh
set -eu

: "${CODEX_HOME:=/data/codex}"
export CODEX_HOME

mkdir -p "$CODEX_HOME"


setup_gitlab_auth() {
    # Allows using the same image for one-off `codex login` without GitLab env.
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

    # 1. Authenticate glab for GitLab API operations.
    printf '%s' "$GITLAB_TOKEN" | "$glab_bin" auth login \
        --hostname "$gitlab_host" \
        --git-protocol https \
        --stdin \
        >/dev/null

    if ! "$glab_bin" auth status --hostname "$gitlab_host" >/dev/null 2>&1; then
        echo "GitLab authentication check failed for ${gitlab_host}" >&2
        exit 1
    fi

    # Resolve the real username associated with the access token.
    # For Project Access Tokens this is usually project_<id>_bot_<suffix>.
    gitlab_username="$(
        "$glab_bin" api \
            --hostname "$gitlab_host" \
            user \
            --jq '.username'
    )"

    if [ -z "$gitlab_username" ]; then
        echo "Could not resolve GitLab username for ${gitlab_host}" >&2
        exit 1
    fi

    # 2. Remove credential configuration left by older image versions.
    #
    # Old versions used:
    #   - forced username "oauth2"
    #   - `glab auth git-credential`
    #
    # Both are intentionally removed here.
    git config --global --unset-all \
        "credential.https://${gitlab_host}.username" \
        >/dev/null 2>&1 || true

    git config --global --unset-all \
        "credential.https://${gitlab_host}.helper" \
        >/dev/null 2>&1 || true

    git config --global --unset-all \
        "credential.username" \
        >/dev/null 2>&1 || true

    git config --global --unset-all \
        "credential.helper" \
        >/dev/null 2>&1 || true

    # 3. Store the SAME GitLab access token using Git's standard HTTPS
    # credential store. This lets both the Python orchestrator and Codex run
    # ordinary `git clone`, `git fetch` and `git push` without receiving the
    # token in their process environment.
    git config --global credential.helper store

    printf 'protocol=https\nhost=%s\nusername=%s\npassword=%s\n\n' \
        "$gitlab_host" \
        "$gitlab_username" \
        "$GITLAB_TOKEN" \
        | git credential approve

    # Protect the credential file from other Unix users in the container.
    if [ -f "$HOME/.git-credentials" ]; then
        chmod 0600 "$HOME/.git-credentials"
    fi

    # Deterministic identity for autonomous commits unless overridden by repo.
    git config --global user.name \
        "${GIT_AUTHOR_NAME:-Codex Orchestrator}"

    git config --global user.email \
        "${GIT_AUTHOR_EMAIL:-codex-orchestrator@localhost}"
}


setup_gitlab_auth

exec /usr/bin/tini -- "$@"