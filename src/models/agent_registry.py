"""Agent registry and proposal review models."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, SmallInteger, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class AgentRegistry(Base):
    __tablename__ = "agents"
    __table_args__ = (
        Index("ix_agents_approved_by", "approved_by"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        unique=True,
        nullable=True,
    )
    bot_name: Mapped[str] = mapped_column(String(100), nullable=False)
    pi_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )  # pending, active, suspended, inactive (parked: excluded from sim runs, reversible)
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pi_lab", default="pi_lab"
    )  # selects per-role prompts + tool allow-list; 'pi_lab' == legacy behaviour
    slack_bot_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    slack_user_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    delegate_slack_ids: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Relationships
    user: Mapped["User | None"] = relationship(
        "User", foreign_keys=[user_id], back_populates="agent"
    )
    delegates: Mapped[list["AgentDelegate"]] = relationship(
        "AgentDelegate", back_populates="agent", cascade="all, delete-orphan", passive_deletes=True
    )
    invitations: Mapped[list["DelegateInvitation"]] = relationship(
        "DelegateInvitation",
        foreign_keys="DelegateInvitation.agent_registry_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<AgentRegistry agent_id={self.agent_id} status={self.status}>"


#: The two `ProposalReview.rating` values that are MARKERS, not a PI's answer.
#:
#: -1 is the engine's implicit marker: `simulation.py` writes it the first time a PI
#: engages a proposal thread, so the rebuild does not re-block the proposal (#20 COR-5).
#: 0 is the reopen-with-guidance sentinel the web route writes (#20 COR-13), and it is
#: also what a 2026-04-30/05-01 bulk backfill left on 227 rows that carry no comment and
#: no `reviewed_by_user_id`.
#:
#: A real review is 1..4 — the only range the form offers, the only range
#: `agent_page.py` accepts, and the only range `email_inbound.py` accepts.
#:
#: EVERY predicate that asks either "has this been reviewed?" or "may I upgrade this row
#: in place?" must use this tuple. An audit of this branch found the two questions had
#: drifted apart: the notification sweep had learned to exclude 0 while the reply path
#: still asked `rating != -1`, so 11 of 26 notifiable users were reminded forever,
#: answered "Got it - you rated it 4", and never recorded. One constant, so the two
#: agree by construction rather than by coincidence.
REVIEW_MARKER_RATINGS = (-1, 0)


class ProposalReview(Base):
    __tablename__ = "proposal_reviews"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    thread_decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("thread_decisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[str] = mapped_column(String(50), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    delegate_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_via: Mapped[str] = mapped_column(
        String(10), nullable=False, default="web"
    )  # web, email, engine (implicit rating=-1 marker, upgraded in place by the
    # first explicit action)
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    thread_decision: Mapped["ThreadDecision"] = relationship("ThreadDecision")

    __table_args__ = (
        Index("ix_proposal_reviews_user_id", "user_id"),
        Index("ix_proposal_reviews_delegate_user_id", "delegate_user_id"),
        Index("ix_proposal_reviews_reviewed_by_user_id", "reviewed_by_user_id"),
        # Each agent can only review a thread decision once
        {"comment": "unique constraint on (thread_decision_id, agent_id) added in migration"},
    )

    def __repr__(self) -> str:
        return f"<ProposalReview agent={self.agent_id} rating={self.rating}>"
