"""One hub decline that carried no assessment (migration 0071).

The hub closes most interviews with a ⏸️ decline and no ``<assessment_json>`` sidecar
(phase4-thread-reply.md Outcome 2), so the interview left no ``opportunity_assessments``
row and appeared nowhere: not on the Assessments page, not in ``#assessments-summary``.
Owner decision 2026-10-16: record every such close here, list it on the Assessments
page and post a one-line headline for it, without changing the hub prompt.

One row per interview (``uq_hub_declines_run_thread``). ``reason_text`` is the hub's
closing reply as posted to the interview thread (public to the workspace already), at
most ``REASON_MAX_CHARS``. ``summary_claimed_at`` / ``summary_posted_at`` follow the
assessment headline's claim protocol (``src/services/decline_claims.py``): claim, post,
then mark posted or release. A row whose interview also has an assessment is never
written: the assessment is that interview's record.

No relationship is declared on ``SimulationRun``: deleting a run removes its rows
through the database's ON DELETE CASCADE.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

#: Slack's per-message limit; a closing reply longer than this was split, and its first
#: chunk's text is the part kept.
REASON_MAX_CHARS = 4000


class HubDecline(Base):
    __tablename__ = "hub_declines"
    __table_args__ = (
        # Led by simulation_run_id: serves the run cascade and every per-run read
        # (tests/unit/test_run_scoped_tables.py).
        UniqueConstraint("simulation_run_id", "thread_id", name="uq_hub_declines_run_thread"),
        CheckConstraint(
            "char_length(reason_text) <= 4000", name="ck_hub_declines_reason_length"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    simulation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("simulation_runs.id", ondelete="CASCADE"), nullable=False
    )
    #: The hub that declined.
    agent_id: Mapped[str] = mapped_column(String(50), nullable=False)
    #: The lab whose pitch was declined.
    subject_agent_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    channel_name: Mapped[str] = mapped_column(String(100), nullable=False)
    #: The interview's canonical thread id (its root post).
    thread_id: Mapped[str] = mapped_column(String(50), nullable=False)
    #: The Slack ts of the hub's closing reply, for the headline's permalink; NULL when
    #: the reply was not mirrored to Slack.
    slack_ts: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reason_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    summary_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    summary_posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
