import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from .models import Job


def _now() -> str:
    return datetime.now(UTC).isoformat()


class StateDB:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    project_id INTEGER NOT NULL,
                    issue_iid INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    session_id TEXT,
                    workspace TEXT,
                    last_note_id INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, issue_iid)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state)")

    @staticmethod
    def _row_to_job(row: sqlite3.Row | None) -> Job | None:
        if row is None:
            return None
        return Job(
            project_id=row["project_id"],
            issue_iid=row["issue_iid"],
            state=row["state"],
            session_id=row["session_id"],
            workspace=row["workspace"],
            last_note_id=row["last_note_id"],
            last_error=row["last_error"],
        )

    def get(self, project_id: int, issue_iid: int) -> Job | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE project_id=? AND issue_iid=?",
                (project_id, issue_iid),
            ).fetchone()
            return self._row_to_job(row)

    def claim_initial(self, project_id: int, issue_iid: int) -> bool:
        """Atomically create the job. Duplicate webhook deliveries lose the insert."""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                now = _now()
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO jobs
                    (project_id, issue_iid, state, created_at, updated_at)
                    VALUES (?, ?, 'running_initial', ?, ?)
                    """,
                    (project_id, issue_iid, now, now),
                )
                acquired = cur.rowcount == 1
                if not acquired:
                    # A failed issue may be explicitly retried by adding the trigger
                    # label again. Start a fresh Codex thread but keep the same row.
                    cur = conn.execute(
                        """
                        UPDATE jobs
                        SET state='running_initial', session_id=NULL, last_note_id=0,
                            last_error=NULL, updated_at=?
                        WHERE project_id=? AND issue_iid=? AND state='failed'
                        """,
                        (now, project_id, issue_iid),
                    )
                    acquired = cur.rowcount == 1
                conn.execute("COMMIT")
                return acquired
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def claim_resume(self, project_id: int, issue_iid: int) -> bool:
        """CAS WAITING/DONE -> RUNNING_RESUME. Only one duplicate event can win.

        DONE is intentionally resumable: a human can add a follow-up comment and
        `ai::resume` to continue the same persisted Codex thread.
        """
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    """
                    UPDATE jobs
                    SET state='running_resume', last_error=NULL, updated_at=?
                    WHERE project_id=? AND issue_iid=? AND state IN ('waiting', 'done')
                    """,
                    (_now(), project_id, issue_iid),
                )
                conn.execute("COMMIT")
                return cur.rowcount == 1
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def update(
        self,
        project_id: int,
        issue_iid: int,
        *,
        state: str | None = None,
        session_id: str | None = None,
        workspace: str | None = None,
        last_note_id: int | None = None,
        last_error: str | None = None,
    ) -> None:
        values: list[object] = []
        fields: list[str] = []
        for name, value in (
            ("state", state),
            ("session_id", session_id),
            ("workspace", workspace),
            ("last_note_id", last_note_id),
            ("last_error", last_error),
        ):
            if value is not None:
                fields.append(f"{name}=?")
                values.append(value)
        fields.append("updated_at=?")
        values.append(_now())
        values.extend([project_id, issue_iid])
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE jobs SET {', '.join(fields)} WHERE project_id=? AND issue_iid=?",
                values,
            )

    def fail(self, project_id: int, issue_iid: int, error: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs SET state='failed', last_error=?, updated_at=?
                WHERE project_id=? AND issue_iid=?
                """,
                (error[:8000], _now(), project_id, issue_iid),
            )

    def recover_interrupted(self) -> list[tuple[int, int, str]]:
        """Return jobs interrupted by a process/container restart.

        They remain in their running state. The orchestrator can safely re-enter:
        if a thread_id exists it resumes that thread, otherwise it starts a new one.
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT project_id, issue_iid, state FROM jobs WHERE state IN ('running_initial','running_resume')"
            ).fetchall()
        return [(r["project_id"], r["issue_iid"], r["state"]) for r in rows]
