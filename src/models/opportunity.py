"""Durable store for BlackbirdBot's screening verdicts.

Before this table an assessment existed only as a Slack message: nothing was
queryable, nothing was rankable, and the machine-readable verdict the rubric
(Part C.6) calls for was never emitted at all. One row per posted :mag:
Opportunity Assessment.

Every rubric field is nullable because a sparse or partly-unparseable verdict
must still be recorded — losing the assessment is strictly worse than storing an
incomplete one.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class OpportunityAssessment(Base):
    __tablename__ = "opportunity_assessments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    simulation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("simulation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The scouting agent (blackbird) and the lab it assessed.
    agent_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    subject_agent_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    channel_name: Mapped[str] = mapped_column(String(100), nullable=False)
    slack_ts: Mapped[str | None] = mapped_column(String(50), nullable=True)

    company_or_project: Mapped[str | None] = mapped_column(Text, nullable=True)
    funnel_stage: Mapped[str | None] = mapped_column(String(20), nullable=True)
    recommendation: Mapped[str | None] = mapped_column(String(30), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Computed by src/services/blackbird_rubric.py, NOT taken from the model.
    weighted_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    band: Mapped[str | None] = mapped_column(String(20), nullable=True)

    gating: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    scores: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    red_flags: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    derisking_milestones: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The verdict exactly as emitted, so a schema change never loses the original.
    raw_verdict: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<OpportunityAssessment subject={self.subject_agent_id} "
            f"rec={self.recommendation} score={self.weighted_score}>"
        )
