from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field


JobState = Literal[
    "running_initial",
    "waiting",
    "running_resume",
    "done",
    "failed",
]


@dataclass(slots=True)
class Job:
    project_id: int
    issue_iid: int
    state: str
    session_id: str | None
    workspace: str | None
    last_note_id: int
    last_error: str | None


class CodexResult(BaseModel):
    status: Literal["needs_input", "complete"]
    summary: str = Field(min_length=1)
    analysis: str = Field(default="")
    questions: list[str] = Field(default_factory=list)
