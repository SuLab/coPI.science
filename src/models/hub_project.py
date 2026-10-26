"""BlackbirdBot projects: research, postings, refinement and their reviews (migration 0073).

Four tables back the projects prototype (docs/specs/2026-10-22-idea-pitching-prototype-design.md):

- ``hub_projects``: one row per project. The engine requests one for each live lab once per
  ``PROJECT_INTERVAL_HOURS`` of run time (``uq_hub_projects_run_agent_slot``: one row per run,
  lab and slot, so a restart cannot request twice), the worker researches it and stores the
  choice and the posting text, the engine posts it and offers the lab its reply, and the close
  of the project thread records the outcome. Every status change is a compare-and-set
  (``UPDATE … WHERE status = <expected>``), so the engine and the worker never overwrite each
  other.
- ``hub_project_calls``: the worker's per-call ledger, one row per model call, claimed before
  the call so the rolling 24-hour dollar ceiling counts a call the worker died in.
- ``hub_project_clocks``: one row per run holding its run time in seconds, the clock the
  once-per-interval rule counts.
- ``hub_project_reviews``: staff and reviewer reviews of a project (a 1–5 rating, a comment,
  fund or don't fund), kept apart from assessment reviews.

No relationship is declared on ``SimulationRun``: deleting a run removes its projects (and,
through ``hub_projects``, their calls and reviews) through the database's ON DELETE CASCADE.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

STATUS_REQUESTED = "requested"
STATUS_RESEARCHING = "researching"
STATUS_READY_TO_POST = "ready_to_post"
STATUS_POSTED = "posted"
STATUS_REFINING = "refining"
STATUS_READY = "ready"
STATUS_NOT_AGREED = "not_agreed"
STATUS_NOT_FINISHED = "not_finished"
#: A project in one of these is still in progress (or paused, when its run is not live).
OPEN_STATUSES = (
    STATUS_REQUESTED,
    STATUS_RESEARCHING,
    STATUS_READY_TO_POST,
    STATUS_POSTED,
    STATUS_REFINING,
)
FINISHED_STATUSES = (STATUS_READY, STATUS_NOT_AGREED, STATUS_NOT_FINISHED)

#: Why a project is ``not_finished`` (``hub_projects.reason_code``, String(40)).
REASON_NO_IDEA = "no_idea"
REASON_RESEARCH_FAILED = "research_failed"
REASON_CHOICE_FAILED = "choice_failed"
REASON_RUN_ENDED = "run_ended_before_posting"
REASON_LAB_NOT_LIVE = "lab_not_live"
REASON_REPLY_FAILED = "reply_failed"
REASON_THREAD_TIMEOUT = "thread_timeout"
REASON_THREAD_ABANDONED = "thread_abandoned"
REASON_CLOSE_UNREADABLE = "close_unreadable"
REASONS = (
    REASON_NO_IDEA,
    REASON_RESEARCH_FAILED,
    REASON_CHOICE_FAILED,
    REASON_RUN_ENDED,
    REASON_LAB_NOT_LIVE,
    REASON_REPLY_FAILED,
    REASON_THREAD_TIMEOUT,
    REASON_THREAD_ABANDONED,
    REASON_CLOSE_UNREADABLE,
)

#: ``hub_project_calls.status`` values.
CALL_IN_PROGRESS = "in_progress"
CALL_DONE = "done"
CALL_FAILED = "failed"
CALL_REFUSED = "refused"

#: The ``thread_decisions.outcome`` a project thread closes with (``thread_outcome_enum``,
#: added by migration 0073).
THREAD_OUTCOME_PROJECT_CLOSED = "project_closed"


class HubProject(Base):
    __tablename__ = "hub_projects"
    __table_args__ = (
        # Led by simulation_run_id: serves the run cascade and every per-run read
        # (tests/unit/test_run_scoped_tables.py), and is what makes a request idempotent.
        UniqueConstraint(
            "simulation_run_id", "agent_id", "slot", name="uq_hub_projects_run_agent_slot"
        ),
        CheckConstraint(
            "status IN ('requested','researching','ready_to_post','posted','refining',"
            "'ready','not_agreed','not_finished')",
            name="ck_hub_projects_status",
        ),
        # A project thread finds its project by the lab reply's id.
        Index("ix_hub_projects_run_reply", "simulation_run_id", "reply_ts"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    simulation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("simulation_runs.id", ondelete="CASCADE"), nullable=False
    )
    #: The lab's ``agent_registry.agent_id``; no FK, so a deleted lab keeps its projects.
    agent_id: Mapped[str] = mapped_column(String(50), nullable=False)
    #: The lab's sequence number of projects in this run, from 1.
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: The run clock (``hub_project_clocks.run_seconds``) when the project was requested.
    requested_run_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: The worker's research state (sessions, stored conversations, candidates).
    research: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    #: The resolved choice: chosen use, reasons, opening plan, anchors and sources.
    choice: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: The posting as assembled by code from the checked choice; the engine posts it as stored.
    posting_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Set just before the engine posts; resolved from the log or cleared afterwards.
    posting_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posting_ts: Mapped[str | None] = mapped_column(String(50), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Set just before the lab's reply is posted; resolved from the log or cleared afterwards.
    reply_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The lab reply's id, which is the project thread's id.
    reply_ts: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reply_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: What the close recorded: the final plan, tool marks, positions, gaps and disagreements.
    final: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The run clock when the project finished (the engine's clock, or the stored clock row
    #: when the worker finished it).
    finished_run_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: The worker does not take the project's next step before this time.
    next_step_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<HubProject run={self.simulation_run_id} lab={self.agent_id} slot={self.slot} {self.status}>"


class HubProjectCall(Base):
    __tablename__ = "hub_project_calls"
    __table_args__ = (
        CheckConstraint(
            "status IN ('in_progress','done','failed','refused')",
            name="ck_hub_project_calls_status",
        ),
        # The daily spend ceiling sums rows updated in the last 24 hours.
        Index("ix_hub_project_calls_updated", "updated_at"),
        Index("ix_hub_project_calls_project", "project_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hub_projects.id", ondelete="CASCADE"), nullable=False
    )
    #: ``research:<domain>`` or ``choose``.
    step: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    served_by_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: The call's usage entries; an in-flight reserve entry until the call returns.
    usage_by_model: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<HubProjectCall project={self.project_id} {self.step} {self.status}>"


class HubProjectClock(Base):
    __tablename__ = "hub_project_clocks"
    __table_args__ = (
        UniqueConstraint("simulation_run_id", name="uq_hub_project_clocks_run"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    simulation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("simulation_runs.id", ondelete="CASCADE"), nullable=False
    )
    #: Seconds of run time; advanced by a SQL increment, never by writing a cached value.
    run_seconds: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<HubProjectClock run={self.simulation_run_id} {self.run_seconds:.0f}s>"


class HubProjectReview(Base):
    __tablename__ = "hub_project_reviews"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "reviewer_user_id", name="uq_hub_project_reviews_project_reviewer"
        ),
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_hub_project_reviews_rating"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hub_projects.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: The author's name when written, kept after the user is deleted (as assessment reviews do).
    reviewer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    fund: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<HubProjectReview project={self.project_id} rating={self.rating} fund={self.fund}>"
