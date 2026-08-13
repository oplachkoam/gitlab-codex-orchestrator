# GitLab Codex Orchestrator

A small deterministic background service that turns GitLab issues into resumable Codex CLI analysis sessions.

The orchestrator itself does **not** use the OpenAI SDK, Responses API directly, `OPENAI_API_KEY`, or `CODEX_API_KEY`. All model interaction happens inside the Codex CLI, authenticated once with your ChatGPT/Codex account using `codex login`.

## Workflow

1. A GitLab issue gets `ai::ready`.
2. GitLab sends an Issue Hook to the service.
3. The service atomically claims `(project_id, issue_iid)` in SQLite and changes the issue to `ai::analyzing`.
4. It creates an isolated repository clone under `/data/workspaces/<project>/<issue>`.
5. It runs `codex exec --json` in read-only mode and stores the emitted Codex `thread_id`.
6. If Codex needs clarification, it posts questions and changes the issue to `ai::waiting`.
7. A human answers in issue comments and manually adds/replaces the label with `ai::resume`.
8. The service runs `codex exec resume <thread_id> ...`, feeding the new comments into the **same Codex session**.
9. It either asks another round of questions or posts the final analysis and sets `ai::done`.

## Architecture

```text
GitLab Issue Hook
       |
       v
+-----------------------------+
| Python orchestrator         |
|                             |
| FastAPI webhook             |
| SQLite/WAL state machine    |
| GitLab REST client          |
| Git repository manager      |
+-------------+---------------+
              |
              | subprocess
              v
       +-------------+
       | Codex CLI   |
       | ChatGPT auth|
       +------+------+ 
              |
              v
         Codex service
```

There is no LLM decision-making in the orchestration layer. Label transitions, locking, repository preparation, prompt construction, comment collection, retries, and session IDs are all handled deterministically by Python code.

## Requirements

On the host you only need:

- Docker;
- network access to your GitLab instance;
- network access required by Codex;
- a ChatGPT account/workspace with Codex access.

You do **not** need Codex, Node.js, Python, or Git installed on the host. They are inside the image.

## 1. Build the image

```bash
docker build -t gitlab-codex-orchestrator:local .
```

The image contains:

- Python;
- Git;
- Node.js;
- `@openai/codex`;
- `bubblewrap` for Codex sandboxing on Linux;
- the orchestrator itself.

For reproducible production deployments, pin the Codex CLI version you tested:

```bash
docker build \
  --build-arg CODEX_VERSION=<tested-version> \
  -t gitlab-codex-orchestrator:0.2.0 .
```

## 2. Create the persistent volume

The same volume stores:

- `/data/codex/auth.json` — Codex login credentials;
- `/data/codex/config.toml` — Codex configuration;
- `/data/state.db` — orchestrator state;
- `/data/workspaces/...` — per-issue repository clones;
- Codex session data required for `resume`.

Create it once:

```bash
docker volume create gitlab-codex-data
```

Do not recreate or casually delete this volume if you need existing Codex sessions to resume.

## 3. Log Codex in once

For a Docker/headless environment, use Codex device-code login:

```bash
docker run --rm -it \
  -v gitlab-codex-data:/data \
  gitlab-codex-orchestrator:local \
  codex login --device-auth
```

Codex prints a URL and a one-time code. Open the URL on your normal computer, sign in to ChatGPT, and enter the code.

If device-code login is disabled for your account/workspace, enable it in the relevant ChatGPT security/workspace settings first.

The image sets:

```text
CODEX_HOME=/data/codex
```

and forces:

```toml
cli_auth_credentials_store = "file"
```

so the login is written to the persistent Docker volume instead of disappearing with the temporary login container.

Check the login:

```bash
docker run --rm \
  -v gitlab-codex-data:/data \
  gitlab-codex-orchestrator:local \
  codex login status
```

The normal service also checks `codex login status` during startup. If the volume is not logged in, the container fails fast instead of claiming a GitLab issue and failing later.

### Credential lifetime

Codex manages its ChatGPT session itself and can refresh the file-backed login. Because `/data/codex` is persistent, refreshed credentials remain available across container restarts.

Treat `/data/codex/auth.json` like a password. Do not commit, export, or expose it to other containers.

## 4. Configure GitLab

Copy the example:

```bash
cp .env.example .env
```

Edit at least:

```env
GITLAB_URL=https://gitlab.example.com
GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
GITLAB_WEBHOOK_SECRET=replace-with-a-long-random-secret
```

There is intentionally **no** `OPENAI_API_KEY` in the configuration.

The GitLab token must be able to:

- read the repository over HTTPS;
- read issues and notes;
- update issue labels;
- create issue notes.

## 5. Run the service

```bash
docker run -d \
  --name gitlab-codex-orchestrator \
  --restart unless-stopped \
  -p 8080:8080 \
  -v gitlab-codex-data:/data \
  --env-file .env \
  gitlab-codex-orchestrator:local
```

Check logs:

```bash
docker logs -f gitlab-codex-orchestrator
```

Health check:

```bash
curl http://127.0.0.1:8080/healthz
```

Verify Codex login from the running container if needed:

```bash
docker exec gitlab-codex-orchestrator codex login status
```

## 6. Configure the GitLab webhook

Create a project webhook:

```text
URL:          https://your-orchestrator.example.com/webhooks/gitlab
Secret token: same value as GITLAB_WEBHOOK_SECRET
Trigger:      Issues events
```

The orchestrator validates `X-Gitlab-Token` and refetches the issue from the GitLab API before acting, so it does not trust the webhook label snapshot as the source of truth.

## Labels

Defaults:

| Purpose | Label |
|---|---|
| Start analysis | `ai::ready` |
| Codex is running | `ai::analyzing` |
| Waiting for human | `ai::waiting` |
| Resume same thread | `ai::resume` |
| Finished | `ai::done` |
| Failed | `ai::error` |

All names are configurable in `.env`.

## Typical issue flow

```text
bug, backend, ai::ready
        |
        v
bug, backend, ai::analyzing
        |
        v
bug, backend, ai::waiting
```

The orchestrator posts something like:

```markdown
### Codex analysis — clarification needed

I found the existing billing flow in `internal/billing/...`.

#### Questions
1. Should retries be idempotent across process restarts?
2. Is backward compatibility with v1 clients required?
```

Answer using normal GitLab issue comments, then manually add/change the label to:

```text
ai::resume
```

The service reads new non-system comments posted after its previous question, then invokes the stored Codex `thread_id` with `codex exec resume`.

## State and atomicity

SQLite lives at `/data/state.db` in WAL mode. The primary key is:

```text
(project_id, issue_iid)
```

Initial acquisition is an atomic `INSERT OR IGNORE`; continuation is a compare-and-swap transition from `waiting` to `running_resume`.

Duplicate GitLab webhook deliveries therefore cannot start two Codex runs for the same issue inside one service instance. If a label event arrives while that issue is already queued/running, it is coalesced into one follow-up pass rather than silently dropped.

This design intentionally targets **one running container**. For multiple replicas, replace the SQLite claim mechanism with PostgreSQL/advisory locks or another distributed lock.

## Crash recovery

The Codex `thread_id` is persisted as soon as the `thread.started` JSONL event is observed.

If the container restarts during the initial analysis:

- if a `thread_id` was already stored, the service resumes the same session;
- otherwise it restarts the initial Codex turn.

If it restarts during a clarification continuation, it re-enters the stored session and resupplies the clarification context.

The same repository workspace is preserved across clarification rounds.

## Configuration

Important variables:

```env
MAX_WORKERS=2
CODEX_MODEL=
CODEX_REASONING_EFFORT=high
CODEX_SANDBOX=read-only
CODEX_TIMEOUT_SECONDS=1800
GIT_DEPTH=0
```

- `MAX_WORKERS`: number of different issues that may be analyzed concurrently.
- `CODEX_MODEL`: empty means use the Codex CLI default.
- `CODEX_REASONING_EFFORT`: passed to Codex config.
- `CODEX_SANDBOX=read-only`: recommended for this analysis-only workflow.
- `CODEX_TIMEOUT_SECONDS`: hard timeout for one Codex turn.
- `GIT_DEPTH=0`: full repository history; positive number enables shallow clone.

## Security

The orchestrator:

- validates the GitLab webhook secret with constant-time comparison;
- runs Codex with a read-only sandbox by default;
- keeps GitLab credentials out of the Codex subprocess environment;
- keeps Codex authentication in `/data/codex/auth.json`, not environment variables;
- configures Codex shell environment inheritance conservatively;
- does not run repository-owned setup scripts, package installation, tests, or hooks itself;
- uses JSON Schema structured output instead of parsing prose markers.

Repositories and issue contents must still be treated as untrusted prompt input. Keep this service on trusted private infrastructure and do not switch it to unrestricted sandbox access unless you intentionally accept that risk.

## API endpoints

```text
GET  /healthz
POST /webhooks/gitlab
```

There is intentionally no public endpoint that accepts arbitrary Codex prompts. GitLab issue state is the control plane.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

For local Python-only development you can run the state/GitLab tests without a Codex login. The actual service startup requires a valid Codex login because it verifies authentication before starting workers.

## Current boundaries

- One running orchestrator container is supported.
- Repository analysis is read-only; there is no branch creation, commit, push, or merge request creation.
- A completed issue is not automatically restarted by re-adding `ai::ready`.
- A failed issue can be explicitly retried by adding `ai::ready` again; that starts a fresh Codex thread and refreshes the checkout.
- Human answers are read from issue comments after the last orchestrator question note. The current issue description is also supplied on continuation.

## Upstream Codex behavior used by this project

This project relies on Codex CLI behavior documented by OpenAI:

- `codex login --device-auth` supports login on headless machines;
- `codex login status` checks the current login;
- `codex exec` is the non-interactive interface;
- `codex exec --json` emits JSONL including a `thread.started` event with `thread_id`;
- `codex exec resume <SESSION_ID>` resumes a previous non-interactive session;
- file-backed `auth.json` can be refreshed by Codex and should be stored persistently.

Official documentation:

- https://developers.openai.com/codex/auth
- https://developers.openai.com/codex/non-interactive-mode
- https://developers.openai.com/codex/auth/ci-cd-auth
- https://developers.openai.com/codex/sandboxing
