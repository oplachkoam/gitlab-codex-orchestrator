import asyncio
import logging
from pathlib import Path
from typing import Any

from .codex import CodexRunner
from .config import Settings
from .db import StateDB
from .gitlab import GitLabClient
from .models import CodexResult, Job
from .repository import RepositoryManager

log = logging.getLogger(__name__)
BOT_MARKER = "<!-- gitlab-codex-orchestrator -->"


class Orchestrator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = StateDB(settings.db_path)
        self.gitlab = GitLabClient(settings)
        self.repos = RepositoryManager(settings)
        self.codex = CodexRunner(settings)
        self.queue: asyncio.Queue[tuple[int, int]] = asyncio.Queue()
        self.workers: list[asyncio.Task[None]] = []
        self._queued: set[tuple[int, int]] = set()
        self._dirty: set[tuple[int, int]] = set()
        self._queue_lock = asyncio.Lock()

    async def start(self) -> None:
        # Login is an explicit deployment prerequisite. Fail fast instead of
        # discovering missing Codex credentials only after an issue is claimed.
        await self.codex.ensure_authenticated()
        for i in range(self.settings.max_workers):
            self.workers.append(asyncio.create_task(self._worker(i), name=f"worker-{i}"))
        for project_id, issue_iid, _state in self.db.recover_interrupted():
            await self.enqueue(project_id, issue_iid)

    async def stop(self) -> None:
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        await self.gitlab.close()

    async def enqueue(self, project_id: int, issue_iid: int) -> None:
        key = (project_id, issue_iid)
        async with self._queue_lock:
            if key in self._queued:
                # Preserve a transition that arrived while this issue was already
                # queued/running (e.g. the human adds the resume label immediately).
                self._dirty.add(key)
                return
            self._queued.add(key)
            await self.queue.put(key)

    async def _worker(self, worker_id: int) -> None:
        while True:
            project_id, issue_iid = await self.queue.get()
            key = (project_id, issue_iid)
            try:
                await self._process_event(project_id, issue_iid)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker %s failed for %s#%s", worker_id, project_id, issue_iid)
            finally:
                async with self._queue_lock:
                    if key in self._dirty:
                        self._dirty.discard(key)
                        # Keep the key marked as queued while scheduling one coalesced
                        # follow-up pass so we never lose the latest label transition.
                        await self.queue.put(key)
                    else:
                        self._queued.discard(key)
                self.queue.task_done()

    async def _process_event(self, project_id: int, issue_iid: int) -> None:
        issue = await self.gitlab.get_issue(project_id, issue_iid)
        labels = {label if isinstance(label, str) else label.get("name") for label in issue.get("labels", [])}
        labels.discard(None)
        job = self.db.get(project_id, issue_iid)

        # Recovery: a prior container died after atomically claiming the job.
        if job and job.state in {"running_initial", "running_resume"}:
            await self._run_claimed(job, issue, recovering=True)
            return

        if self.settings.resume_label in labels:
            if self.db.claim_resume(project_id, issue_iid):
                claimed = self.db.get(project_id, issue_iid)
                assert claimed is not None
                await self._run_claimed(claimed, issue, recovering=False)
            return

        if self.settings.trigger_label in labels:
            if self.db.claim_initial(project_id, issue_iid):
                claimed = self.db.get(project_id, issue_iid)
                assert claimed is not None
                await self._run_claimed(claimed, issue, recovering=False)
            return

    async def _run_claimed(self, job: Job, issue: dict[str, Any], *, recovering: bool) -> None:
        project_id, issue_iid = job.project_id, job.issue_iid
        is_resume = job.state == "running_resume"
        try:
            await self.gitlab.add_remove_labels(
                project_id,
                issue_iid,
                add=[self.settings.analyzing_label],
                remove=[
                    self.settings.trigger_label,
                    self.settings.resume_label,
                    self.settings.waiting_label,
                    self.settings.error_label,
                ],
            )

            project = await self.gitlab.get_project(project_id)
            repo_url = project.get("http_url_to_repo") or project.get("web_url", "") + ".git"
            branch = project.get("default_branch") or "main"
            workspace = await self.repos.prepare(
                project_id,
                issue_iid,
                repo_url,
                branch,
                # A new/manual retry should analyze the current default branch.
                # Continuations and crash recovery preserve the exact snapshot/cwd
                # associated with the existing Codex thread.
                refresh_existing=not is_resume and not recovering,
            )
            self.db.update(project_id, issue_iid, workspace=str(workspace))

            if is_resume:
                prompt = await self._resume_prompt(job, issue, recovering=recovering)
                session_id = job.session_id
                if not session_id:
                    raise RuntimeError("cannot resume: job has no Codex session_id")
            else:
                if recovering and job.session_id:
                    prompt = self._recovery_prompt(issue)
                    session_id = job.session_id
                else:
                    prompt = self._initial_prompt(issue, project, branch)
                    session_id = None

            async def persist_thread(thread_id: str) -> None:
                self.db.update(project_id, issue_iid, session_id=thread_id)

            thread_id, result = await self.codex.run(
                workspace,
                prompt,
                session_id=session_id,
                on_thread=persist_thread,
            )
            self.db.update(project_id, issue_iid, session_id=thread_id)
            await self._publish_result(project_id, issue_iid, result)
        except Exception as exc:
            log.exception("job failed for %s#%s", project_id, issue_iid)
            self.db.fail(project_id, issue_iid, str(exc))
            try:
                await self.gitlab.add_remove_labels(
                    project_id,
                    issue_iid,
                    add=[self.settings.error_label],
                    remove=[self.settings.analyzing_label],
                )
                await self.gitlab.post_note(
                    project_id,
                    issue_iid,
                    f"{BOT_MARKER}\n### Codex orchestrator error\n\n```text\n{str(exc)[:6000]}\n```",
                )
            except Exception:
                log.exception("failed to publish error to GitLab")

    def _initial_prompt(self, issue: dict[str, Any], project: dict[str, Any], branch: str) -> str:
        return f"""
You are analyzing a GitLab issue against the repository checked out in the current directory.
Do not modify files. Inspect the codebase deeply enough to understand the requested change and its implications.

Project: {project.get('path_with_namespace', project.get('name', 'unknown'))}
Default branch: {branch}
Issue: #{issue.get('iid')} {issue.get('title', '')}
Issue URL: {issue.get('web_url', '')}

Issue description:
---
{issue.get('description') or '(empty)'}
---

Your job:
1. Understand the issue and relevant existing code/architecture.
2. Identify concrete implementation areas, risks, hidden dependencies, and acceptance criteria.
3. If essential information is missing, return status=needs_input and ask only precise questions that block a good implementation plan.
4. If enough information is available, return status=complete with a concise summary and a detailed actionable analysis.
5. Never include private chain-of-thought. Return conclusions, evidence from the repository, and actionable reasoning only.
""".strip()

    def _recovery_prompt(self, issue: dict[str, Any]) -> str:
        return f"""
The orchestrator restarted while the previous turn may have been interrupted.
Continue the same analysis safely. Re-read the current repository state and the GitLab issue context below, then produce the required structured result.
Do not modify files.

Issue #{issue.get('iid')}: {issue.get('title', '')}
Current description:
{issue.get('description') or '(empty)'}
""".strip()

    async def _resume_prompt(self, job: Job, issue: dict[str, Any], *, recovering: bool) -> str:
        notes = await self.gitlab.list_notes(job.project_id, job.issue_iid)
        user_notes: list[str] = []
        for note in notes:
            if int(note.get("id", 0)) <= job.last_note_id:
                continue
            if note.get("system"):
                continue
            body = str(note.get("body") or "")
            if BOT_MARKER in body:
                continue
            author = (note.get("author") or {}).get("username") or (note.get("author") or {}).get("name") or "user"
            user_notes.append(f"[{author}] {body}")

        answers = "\n\n".join(user_notes) if user_notes else "(No new non-system comments were found.)"
        prefix = "The orchestrator restarted during this continuation. " if recovering else ""
        return f"""
{prefix}Continue the SAME Codex session. The human has reviewed your previous questions and manually requested continuation.
Do not modify files. Re-check repository files when useful.

Current issue title: {issue.get('title', '')}
Current issue description:
---
{issue.get('description') or '(empty)'}
---

New human comments after your last orchestrator note:
---
{answers}
---

Use these clarifications to continue the previous analysis. If essential questions remain, return needs_input with only the remaining blocking questions. Otherwise return complete with the final actionable analysis.
Do not reveal private chain-of-thought.
""".strip()

    async def _publish_result(self, project_id: int, issue_iid: int, result: CodexResult) -> None:
        if result.status == "needs_input" and result.questions:
            q = "\n".join(f"{i}. {question}" for i, question in enumerate(result.questions, 1))
            body = (
                f"{BOT_MARKER}\n### Codex analysis — clarification needed\n\n"
                f"{result.summary}\n\n"
                f"{result.analysis}\n\n"
                f"#### Questions\n{q}\n\n"
                f"Reply in issue comments, then replace/add label `{self.settings.resume_label}` to continue the same Codex session."
            )
            note = await self.gitlab.post_note(project_id, issue_iid, body)
            self.db.update(project_id, issue_iid, state="waiting", last_note_id=int(note["id"]))
            await self.gitlab.add_remove_labels(
                project_id,
                issue_iid,
                add=[self.settings.waiting_label],
                remove=[self.settings.analyzing_label, self.settings.resume_label],
            )
            return

        body = (
            f"{BOT_MARKER}\n### Codex analysis — complete\n\n"
            f"**Summary:** {result.summary}\n\n"
            f"{result.analysis}"
        )
        note = await self.gitlab.post_note(project_id, issue_iid, body)
        self.db.update(project_id, issue_iid, state="done", last_note_id=int(note["id"]))
        await self.gitlab.add_remove_labels(
            project_id,
            issue_iid,
            add=[self.settings.done_label],
            remove=[self.settings.analyzing_label, self.settings.waiting_label, self.settings.resume_label],
        )
