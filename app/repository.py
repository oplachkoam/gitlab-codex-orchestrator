import asyncio
import os
import shutil
from pathlib import Path

from .config import Settings


class RepositoryError(RuntimeError):
    pass


class RepositoryManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _git_env(self) -> dict[str, str]:
        # Git authentication is provided by the global glab credential helper
        # configured by docker-entrypoint.sh. No token is injected here.
        allow = ["PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"]
        env = {k: os.environ[k] for k in allow if k in os.environ}
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    async def _run(self, args: list[str], cwd: Path | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd) if cwd else None,
            env=self._git_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RepositoryError(
                f"command failed ({proc.returncode}): {' '.join(args)}\n"
                f"{err.decode(errors='replace')[-4000:]}"
            )
        return out.decode(errors="replace")

    def workspace_for(self, project_id: int, issue_iid: int) -> Path:
        return self.settings.workspaces_dir / str(project_id) / str(issue_iid)

    async def prepare(
        self,
        project_id: int,
        issue_iid: int,
        repo_url: str,
        branch: str,
        *,
        refresh_existing: bool = False,
    ) -> Path:
        workspace = self.workspace_for(project_id, issue_iid)
        workspace.parent.mkdir(parents=True, exist_ok=True)

        if not (workspace / ".git").exists():
            if workspace.exists():
                shutil.rmtree(workspace)
            args = ["git", "clone", "--no-tags", "--single-branch", "--branch", branch]
            if self.settings.git_depth > 0:
                args += ["--depth", str(self.settings.git_depth)]
            args += [repo_url, str(workspace)]
            await self._run(args)
        elif refresh_existing:
            await self._run(["git", "remote", "set-url", "origin", repo_url], cwd=workspace)
            await self._run(["git", "fetch", "--prune", "origin", branch], cwd=workspace)
            await self._run(["git", "reset", "--hard", f"origin/{branch}"], cwd=workspace)
            await self._run(["git", "clean", "-fdx"], cwd=workspace)

        return workspace
