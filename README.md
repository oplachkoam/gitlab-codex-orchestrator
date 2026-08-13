# GitLab Codex Orchestrator

Deterministic background orchestrator for GitLab Issues + resumable Codex CLI sessions.

The Python service itself does not call OpenAI APIs. It handles webhooks, labels, SQLite state, repository checkout and Codex thread IDs. All model work is performed by Codex CLI authenticated with `codex login`.

## Workflow

```text
ai::ready
   ↓ GitLab webhook
ai::analyzing
   ↓
Codex: inspect → implement → test → commit → push feature branch
   ├─ blocking question → ai::waiting
   │                         ↓ human comment + ai::resume
   │                      same Codex thread
   │
   └─ complete → ai::done
                  ↓
              ai::resume is allowed again for follow-up work
```

`ai::done` is not destructive: `thread_id` and workspace remain persisted. Add a new issue comment and `ai::resume` to continue the same Codex session.

## Authentication model

There are exactly two independent authentications, but only one GitLab access token:

```text
ChatGPT / Codex
  └─ codex login --device-auth
     └─ /data/codex/auth.json

GitLab
  └─ GITLAB_TOKEN
     └─ glab auth login at container startup
        ├─ Python GitLab REST client uses the same env token
        └─ git clone/fetch/push use glab auth git-credential
```

The GitLab token is not copied into the Codex process environment. Codex can still perform normal HTTPS `git push` because Git invokes the configured `glab auth git-credential` helper.

Because Codex runs with `danger-full-access` inside the Docker security boundary, treat the whole container as trusted: Codex can invoke `glab` and therefore can use the configured GitLab identity. Use a project-scoped token where possible.

## GitLab token

Recommended: Project Access Token with role `Developer` and scopes:

```text
api
write_repository
```

One token is enough for Issue API operations and HTTPS Git push.

## Build

```bash
docker build \
  --build-arg CODEX_VERSION=latest \
  --build-arg GLAB_VERSION=1.109.0 \
  -t gitlab-codex-orchestrator:local \
  .
```

The image contains Python, Git, Node.js, Codex CLI and GitLab CLI (`glab`).

The Dockerfile forces IPv4 for apt and configures retries/timeouts because some Docker/VPN environments stall on Debian IPv6 mirrors.

## Codex config at image build time

Edit `codex-config.toml` before `docker build`:

```toml
approval_policy = "never"
sandbox_mode = "danger-full-access"
model_reasoning_effort = "high"
cli_auth_credentials_store = "file"
```

It is copied into:

```text
/etc/codex/config.toml
```

`app/codex.py` no longer hardcodes model/sandbox/reasoning flags, so these settings belong to Codex configuration rather than orchestrator `.env`.

If your persistent volume already contains `/data/codex/config.toml`, remember that user-level Codex config can override applicable system settings. Remove or update stale settings there if needed.

## Persistent volume

```bash
docker volume create gitlab-codex-data
```

It stores:

```text
/data/codex/          Codex auth + sessions
/data/state.db        SQLite orchestrator state
/data/workspaces/     per-issue repository clones
/data/results/        temporary structured Codex results
```

## Codex login

One-time headless login:

```bash
docker run --rm -it \
  -v gitlab-codex-data:/data \
  gitlab-codex-orchestrator:local \
  codex login --device-auth
```

Check it:

```bash
docker run --rm \
  -v gitlab-codex-data:/data \
  gitlab-codex-orchestrator:local \
  codex login status
```

No `OPENAI_API_KEY` or `CODEX_API_KEY` is required.

## Configure

```bash
cp .env.example .env
```

Minimum:

```env
GITLAB_URL=https://gitlab.example.com
GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
GITLAB_WEBHOOK_SECRET=replace-with-a-long-random-secret
```

Optional Git identity:

```env
GIT_AUTHOR_NAME=Codex Orchestrator
GIT_AUTHOR_EMAIL=codex-orchestrator@localhost
```

## Run

```bash
docker run -d \
  --name gitlab-codex-orchestrator \
  --restart unless-stopped \
  -p 8080:8080 \
  -v gitlab-codex-data:/data \
  --env-file .env \
  gitlab-codex-orchestrator:local
```

Logs:

```bash
docker logs -f gitlab-codex-orchestrator
```

Health:

```bash
curl http://127.0.0.1:8080/healthz
```

Verify GitLab auth from the running container without exposing the token:

```bash
docker exec gitlab-codex-orchestrator glab auth status
```

## What entrypoint does with GitLab auth

At every normal container start, if `GITLAB_TOKEN` is present:

1. parses the host from `GITLAB_URL`;
2. runs non-interactive `glab auth login --stdin`;
3. configures `oauth2` as the HTTPS token username;
4. registers `glab auth git-credential` as Git's credential helper for that host;
5. configures default Git commit identity;
6. verifies `glab auth status` without printing the token.

A temporary container used only for `codex login` may start without GitLab variables; GitLab setup is skipped in that case.

## Test Git push manually

After the service has cloned an issue workspace:

```bash
docker exec -it gitlab-codex-orchestrator sh
cd /data/workspaces/<project-id>/<issue-iid>
git remote -v
git push -u origin HEAD
```

You should not be prompted for username/password.

## GitLab webhook

Orchestrator endpoint:

```text
POST /webhooks/gitlab
```

Example externally:

```text
https://codex.example.com/webhooks/gitlab
```

Configure the project webhook with the same secret as `GITLAB_WEBHOOK_SECRET` and enable the Issue/Work Item event that produces GitLab `Issue Hook` events. The service refetches the current Issue state through the API before acting.

Only label transitions drive work. Comments alone do not resume Codex; after answering, manually set `ai::resume`.

## Labels

```text
ai::ready      start a new task
ai::analyzing  Codex is currently running
ai::waiting    waiting for blocking human clarification
ai::resume     resume the same Codex thread
ai::done       task completed
ai::error      orchestrator/infrastructure failure
```

## Resume after done

This version intentionally supports:

```text
ai::done → comment → ai::resume → ai::analyzing
```

The same stored `thread_id` and workspace are reused. This is useful when Codex completed too early or you want a follow-up adjustment without losing context.

## Prompt behavior

Russian templates live in:

```text
app/prompts.py
```

For code-changing Issues they explicitly require Codex to:

1. inspect repository + applicable `AGENTS.md`;
2. use a feature branch;
3. implement instead of merely proposing a plan;
4. run relevant tests/checks;
5. commit;
6. push the feature branch;
7. return `complete` only after successful implementation and push.

The prompts explicitly forbid asking the user for the GitLab token or printing stored credentials.

## State / atomicity

SQLite uses `(project_id, issue_iid)` as the primary key and WAL mode.

Initial claims are atomic. Resume is a CAS transition from either `waiting` or `done` to `running_resume`, so duplicate webhooks cannot start two turns for one Issue in a single orchestrator instance.

Interrupted `running_initial` / `running_resume` jobs are recovered after restart. The Codex `thread_id` is persisted as soon as the `thread.started` JSON event is observed.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```
