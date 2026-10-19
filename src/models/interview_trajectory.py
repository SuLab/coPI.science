"""One interview's model-written trajectory lines (migration 0072).

The Interviews page (``/workspace/interviews``, staff only) shows one line per post of
each interview. The worker writes those lines once the interview is done
(``src/services/trajectory_generation.py``), one row per run and interview
(``uq_interview_trajectories_run_thread``); until a row is ``ready``, and for any post the
row has no line for, the page shows the post's first sentence instead.

``lines`` maps ``str(first_row_id)`` — the ``agent_messages.id`` of a post's first chunk
(``src/services/interview_posts.py``) — to its line. ``posts_sha256_12`` hashes the
interview's post-id list at generation time; a mismatch with today's list means posts
landed after the line set was written, so the page falls back and the worker
regenerates. The claim protocol, attempts and usage ledger follow
``AssessmentChatSuggestionSet``: a row is written before the model call (``failed`` /
``in_progress``), so a paid attempt is counted even if the worker dies mid-call.

No relationship is declared on ``SimulationRun``: deleting a run removes its rows
through the database's ON DELETE CASCADE.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

TRAJECTORY_STATUS_READY = "ready"
TRAJECTORY_STATUS_FAILED = "failed"
TRAJECTORY_STATUS_REFUSED = "refused"
TRAJECTORY_STATUSES = (
    TRAJECTORY_STATUS_READY,
    TRAJECTORY_STATUS_FAILED,
    TRAJECTORY_STATUS_REFUSED,
)


class InterviewTrajectory(Base):
    __tablename__ = "interview_trajectories"
    __table_args__ = (
        # Led by simulation_run_id: serves the run cascade and every per-run read
        # (tests/unit/test_run_scoped_tables.py), and is the claim's upsert target.
        UniqueConstraint(
            "simulation_run_id", "thread_id", name="uq_interview_trajectories_run_thread"
        ),
        CheckConstraint(
            "status IN ('ready','failed','refused')", name="ck_interview_trajectories_status"
        ),
        # The daily spend ceiling sums rows updated in the last 24 hours.
        Index("ix_interview_trajectories_updated", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    simulation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("simulation_runs.id", ondelete="CASCADE"), nullable=False
    )
    #: The interview's canonical thread id (its root post's ``message_ts``).
    thread_id: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    #: ``{str(first_row_id): line}``; NULL until a reply has been parsed.
    lines: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    served_by_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_sha256_12: Mapped[str] = mapped_column(String(12), nullable=False)
    posts_sha256_12: Mapped[str] = mapped_column(String(12), nullable=False)
    #: Every attempt's usage entries, accumulated (the suggestions ledger's shape).
    usage_by_model: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Set by the claim and result upserts with ``clock_timestamp()``; no onupdate.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<InterviewTrajectory run={self.simulation_run_id} thread={self.thread_id} {self.status}>"
