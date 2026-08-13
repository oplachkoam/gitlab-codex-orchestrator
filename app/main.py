import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request

from .config import Settings
from .orchestrator import Orchestrator

settings = Settings()
settings.ensure_dirs()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
orch = Orchestrator(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await orch.start()
    try:
        yield
    finally:
        await orch.stop()


app = FastAPI(title="GitLab Codex Orchestrator", version="0.2.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/gitlab")
async def gitlab_webhook(
    request: Request,
    x_gitlab_token: str | None = Header(default=None),
    x_gitlab_event: str | None = Header(default=None),
) -> dict[str, str]:
    if not x_gitlab_token or not hmac.compare_digest(x_gitlab_token, settings.gitlab_webhook_secret):
        raise HTTPException(status_code=401, detail="invalid webhook token")

    payload = await request.json()
    if (x_gitlab_event or "") != "Issue Hook" and payload.get("object_kind") != "issue":
        return {"status": "ignored"}

    project_id = (payload.get("project") or {}).get("id") or payload.get("project_id")
    issue_iid = (payload.get("object_attributes") or {}).get("iid")
    if not project_id or not issue_iid:
        raise HTTPException(status_code=400, detail="missing project id or issue iid")

    await orch.enqueue(int(project_id), int(issue_iid))
    return {"status": "accepted"}
