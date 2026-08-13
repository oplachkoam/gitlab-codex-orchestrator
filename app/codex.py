import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from .config import Settings
from .models import CodexResult


class CodexError(RuntimeError):
    pass


ThreadCallback = Callable[[str], Awaitable[None]]


class CodexRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.schema_path = Path(__file__).resolve().parent.parent / "schema" / "codex_result.schema.json"

    def _env(self) -> dict[str, str]:
        # Codex authenticates from $CODEX_HOME/auth.json. Deliberately do not
        # inherit GitLab or arbitrary host/container secrets into agent commands.
        allow = [
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "CODEX_CA_CERTIFICATE",
        ]
        env = {k: os.environ[k] for k in allow if k in os.environ}
        env["CODEX_HOME"] = str(self.settings.codex_home)
        return env

    async def ensure_authenticated(self) -> None:
        """Fail fast unless this persistent CODEX_HOME has a valid Codex login."""
        proc = await asyncio.create_subprocess_exec(
            "codex",
            "login",
            "status",
            env=self._env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            details = (err or out).decode(errors="replace").strip()
            suffix = f" Details: {details[-2000:]}" if details else ""
            raise CodexError(
                "Codex is not logged in for this /data volume. "
                "Run the one-off `codex login --device-auth` container described in README.md first."
                + suffix
            )

    async def run(
        self,
        workspace: Path,
        prompt: str,
        *,
        session_id: str | None = None,
        on_thread: ThreadCallback | None = None,
    ) -> tuple[str, CodexResult]:
        result_path = self.settings.results_dir / f"{uuid.uuid4().hex}.json"
        cmd = [
            "codex",
            "exec",
            "--json",
            "--sandbox",
            self.settings.codex_sandbox,
            "--output-schema",
            str(self.schema_path),
            "--output-last-message",
            str(result_path),
            "-c",
            f'model_reasoning_effort="{self.settings.codex_reasoning_effort}"',
            "-c",
            'shell_environment_policy.inherit="core"',
            "-c",
            "shell_environment_policy.ignore_default_excludes=false",
        ]
        if self.settings.codex_model:
            cmd += ["--model", self.settings.codex_model]
        if session_id:
            cmd += ["resume", session_id, prompt]
        else:
            cmd += [prompt]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workspace),
            env=self._env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        discovered_thread = session_id
        stderr_chunks: list[bytes] = []

        async def consume_stdout() -> None:
            nonlocal discovered_thread
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "thread.started" and event.get("thread_id"):
                    discovered_thread = str(event["thread_id"])
                    if on_thread:
                        await on_thread(discovered_thread)

        async def consume_stderr() -> None:
            assert proc.stderr is not None
            async for raw in proc.stderr:
                stderr_chunks.append(raw)
                if sum(map(len, stderr_chunks)) > 128_000:
                    del stderr_chunks[: len(stderr_chunks) // 2]

        try:
            async with asyncio.timeout(self.settings.codex_timeout_seconds):
                await asyncio.gather(consume_stdout(), consume_stderr(), proc.wait())
        except TimeoutError as exc:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise CodexError(f"codex timed out after {self.settings.codex_timeout_seconds}s") from exc
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
            raise
        except Exception:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise

        if proc.returncode != 0:
            err = b"".join(stderr_chunks).decode(errors="replace")[-12000:]
            raise CodexError(f"codex exited with code {proc.returncode}: {err}")
        if not discovered_thread:
            raise CodexError("codex output did not contain thread.started/thread_id")
        if not result_path.exists():
            raise CodexError("codex did not write --output-last-message file")

        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            result = CodexResult.model_validate(payload)
        except Exception as exc:
            raw = result_path.read_text(encoding="utf-8", errors="replace")
            raise CodexError(f"invalid structured Codex output: {raw[:8000]}") from exc
        finally:
            result_path.unlink(missing_ok=True)

        return discovered_thread, result
