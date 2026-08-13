from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    gitlab_url: str = Field(description="GitLab base URL, e.g. https://gitlab.example.com")
    gitlab_token: str = Field(description="PAT/project token with api + repository read access")
    gitlab_webhook_secret: str = Field(description="Secret token configured on the GitLab webhook")

    trigger_label: str = "ai::ready"
    analyzing_label: str = "ai::analyzing"
    waiting_label: str = "ai::waiting"
    resume_label: str = "ai::resume"
    done_label: str = "ai::done"
    error_label: str = "ai::error"

    # Codex authenticates exclusively from its persistent login cache in CODEX_HOME.
    # The orchestrator itself has no OpenAI API key and makes no model/API calls.
    codex_model: str = "gpt-5.6-sol"
    codex_reasoning_effort: str = "high"
    codex_sandbox: str = "workspace-write"
    codex_timeout_seconds: int = 3600

    data_dir: Path = Path("/data")
    max_workers: int = 2
    git_depth: int = 0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def codex_home(self) -> Path:
        return self.data_dir / "codex"

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    @property
    def api_base(self) -> str:
        return self.gitlab_url.rstrip("/") + "/api/v4"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        self.codex_home.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
