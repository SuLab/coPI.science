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

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text, func, text
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

    # The specialist floor's finding, recorded rather than enforced by
    # discarding. `_specialist_floor_gap` used to REFUSE an advance/conditional
    # verdict whose panel was never convened — but it runs after the concluding
    # reply is already in Slack, so refusing meant the PI had been told and
    # Blackbird kept nothing. Both production refusals (gordy, 2026-08-17) lost
    # a real conditional verdict that way. Storing it flagged keeps the record
    # and keeps the warning; it does not mean the gap is acceptable.
    #
    # True means "we looked and found a gap". False does NOT mean "the panel
    # was fine" on its own — read it together with `missing_domains` below.
    panel_incomplete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Which domains were missing. THREE states, written by
    # `SimulationEngine._persist_assessment`:
    #   [names] — `panel_incomplete=True`. These domains were owed by the
    #             verdict's own content and never consulted in this interview.
    #   NULL    — `panel_incomplete=False`, panel VERIFIED complete: the floor
    #             was able to check, and found nothing owed and unconsulted
    #             (including the `pass` verdicts no panel is owed for at all).
    #   []      — `panel_incomplete=False`, panel UNVERIFIED: the floor could
    #             not check at all. Either the verdict named no
    #             `subject_agent_id`, or the process had recorded no consult
    #             for anyone (`_floor_verifiable`) — the ordinary state after a
    #             restart, and production's last exit was a SIGKILL. Not
    #             evidence of a gap, so it is not flagged; not evidence of a
    #             complete panel either, so it must never be counted as one.
    # Rows written before 2026-08-19 have NULL for both the verified and the
    # unverified case — that conflation is exactly what [] exists to end.
    missing_domains: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<OpportunityAssessment subject={self.subject_agent_id} "
            f"rec={self.recommendation} score={self.weighted_score}>"
        )


class AssessmentDrop(Base):
    """One verdict that was generated but never became an OpportunityAssessment.

    The counterpart to the table above, and the reason it exists: every way an
    assessment can be lost is silent. The concluding reply has already been
    posted to Slack, the thread closes normally, and the only trace is one
    WARNING line in a container log nobody is tailing — so an empty
    /admin/assessments page is indistinguishable from "no ideas screened yet".

    Recording a drop is strictly best-effort and must never cost the reply that
    already went out, exactly like the assessment write itself.

    ``reason`` is one of:
      * ``specialist_floor``     — HISTORICAL ONLY: an advance/conditional verdict
        whose required panel was never convened (``_specialist_floor_gap``), back
        when that gap meant refusing the verdict outright. As of the fix recorded
        on ``OpportunityAssessment.panel_incomplete`` above, that gap is stored
        flagged instead of discarded, so this path can no longer produce a new
        row — the three that already carry this reason predate the change.
      * ``unparseable_sidecar``  — an ``<assessment_json>`` tag was present but
        yielded no usable verdict (commonly a max_tokens truncation that ate the
        closing tag).
      * ``missing_sidecar``      — the reply concluded, did not decline, and
        carried no sidecar at all.
    """

    __tablename__ = "assessment_drops"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    simulation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("simulation_runs.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    agent_id: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_agent_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reason: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<AssessmentDrop subject={self.subject_agent_id} reason={self.reason}>"
        )
