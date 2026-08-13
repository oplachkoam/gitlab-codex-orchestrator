#!/bin/sh
set -eu

: "${CODEX_HOME:=/data/codex}"
export CODEX_HOME

mkdir -p "$CODEX_HOME"


without_gitlab_token_env() {
    env \
        -u GITLAB_TOKEN \
        -u GITLAB_ACCESS_TOKEN \
        -u OAUTH_TOKEN \
        "$@"
}


setup_gitlab_auth() {
    # The same image is also used for one-off `codex login`.
    # In that mode GitLab variables are intentionally absent.
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

    # Resolve GitLab connection parameters once.
    # Output:
    #   line 1: protocol (https/http)
    #   line 2: hostname
    #   line 3: host[:port]
    gitlab_info="$(
        python - "$GITLAB_URL" <<'PY'
import sys
from urllib.parse import urlparse

raw = sys.argv[1].strip()
parsed = urlparse(raw if "://" in raw else "https://" + raw)

if parsed.scheme not in {"http", "https"}:
    raise SystemExit(f"unsupported GITLAB_URL scheme: {parsed.scheme!r}")

if not parsed.hostname:
    raise SystemExit(f"invalid GITLAB_URL: {raw}")

hostport = parsed.hostname
if parsed.port is not None:
    hostport = f"{hostport}:{parsed.port}"

print(parsed.scheme)
print(parsed.hostname)
print(hostport)
PY
    )"

    gitlab_protocol="$(printf '%s\n' "$gitlab_info" | sed -n '1p')"
    gitlab_hostname="$(printf '%s\n' "$gitlab_info" | sed -n '2p')"
    gitlab_hostport="$(printf '%s\n' "$gitlab_info" | sed -n '3p')"

    # ------------------------------------------------------------------
    # glab authentication
    # ------------------------------------------------------------------
    #
    # GITLAB_TOKEN is deliberately removed from glab's environment here.
    # Otherwise glab warns that the environment variable overrides the
    # credentials being stored by `glab auth login`.
    #
    # The token is supplied only through stdin and persisted in glab config.
    printf '%s' "$GITLAB_TOKEN" | without_gitlab_token_env \
        "$glab_bin" auth login \
        --hostname "$gitlab_hostname" \
        --api-host "$gitlab_hostport" \
        --api-protocol "$gitlab_protocol" \
        --git-protocol "$gitlab_protocol" \
        --stdin \
        >/dev/null

    # Verify the STORED glab login, again without allowing env vars to mask it.
    if ! without_gitlab_token_env \
        "$glab_bin" auth status \
        --hostname "$gitlab_hostname" \
        >/dev/null 2>&1
    then
        echo "Stored glab authentication check failed for ${gitlab_hostname}" >&2
        exit 1
    fi

    # ------------------------------------------------------------------
    # Git HTTPS authentication
    # ------------------------------------------------------------------
    #
    # Do NOT use `glab auth git-credential` here.
    #
    # GitLab explicitly allows any non-empty username when a project/group/
    # personal access token is used as the HTTPS password. `oauth2` is a
    # conventional non-empty username and works fine with Project Access
    # Tokens when Git itself performs HTTP Basic auth.
    #
    # Remove configuration created by previous image versions first.
    git config --global --unset-all \
        "credential.https://${gitlab_hostport}.username" \
        >/dev/null 2>&1 || true

    git config --global --unset-all \
        "credential.https://${gitlab_hostport}.helper" \
        >/dev/null 2>&1 || true

    git config --global --unset-all \
        "credential.username" \
        >/dev/null 2>&1 || true

    git config --global --unset-all \
        "credential.helper" \
        >/dev/null 2>&1 || true

    # This container has a dedicated HOME, so the credential file is isolated
    # from the Docker host. Both the orchestrator and Codex inherit HOME and
    # therefore ordinary git clone/fetch/push can use the same credential.
    git config --global credential.helper store

    # Start from a clean credential file on every container creation/start.
    # The source of truth remains GITLAB_TOKEN from the container environment.
    rm -f "$HOME/.git-credentials"

    printf 'protocol=%s\nhost=%s\nusername=oauth2\npassword=%s\n\n' \
        "$gitlab_protocol" \
        "$gitlab_hostport" \
        "$GITLAB_TOKEN" \
        | git credential approve

    if [ ! -s "$HOME/.git-credentials" ]; then
        echo "Git credential store was not created" >&2
        exit 1
    fi

    chmod 0600 "$HOME/.git-credentials"

    # Autonomous commit identity. A repository may override these locally.
    git config --global user.name \
        "${GIT_AUTHOR_NAME:-Codex Orchestrator}"

    git config --global user.email \
        "${GIT_AUTHOR_EMAIL:-codex-orchestrator@localhost}"
}


setup_gitlab_auth

exec /usr/bin/tini -- "$@"
