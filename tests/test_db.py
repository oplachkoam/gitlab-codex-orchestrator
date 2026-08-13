from pathlib import Path

from app.db import StateDB


def test_initial_claim_is_atomic(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    assert db.claim_initial(1, 42) is True
    assert db.claim_initial(1, 42) is False


def test_resume_is_compare_and_swap(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    assert db.claim_initial(1, 42)
    db.update(1, 42, state="waiting", session_id="session-1", last_note_id=100)
    assert db.claim_resume(1, 42) is True
    assert db.claim_resume(1, 42) is False
    job = db.get(1, 42)
    assert job is not None
    assert job.state == "running_resume"
    assert job.session_id == "session-1"


def test_failed_job_can_be_explicitly_retried(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    assert db.claim_initial(1, 42)
    db.update(1, 42, session_id="session-old")
    db.fail(1, 42, "boom")

    assert db.claim_initial(1, 42) is True
    job = db.get(1, 42)
    assert job is not None
    assert job.state == "running_initial"
    assert job.session_id is None
    assert job.last_note_id == 0
    assert job.last_error is None
