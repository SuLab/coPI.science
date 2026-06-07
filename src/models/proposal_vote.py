"""Public, no-login votes on collaboration-graph proposals.

Distinct from :class:`ProposalReview` (which is an authenticated PI's review of
their own agent's proposal): these are lightweight anonymous reactions captured
from the public graph visualization. A ``voter_token`` (a random id stored in
the visitor's browser) lets a visitor change their vote and attach optional
free-text details to the same row, and provides light dedup without a login.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base

VOTE_UP = "up"      # "Great idea"
VOTE_DOWN = "down"  # "Pass"


class ProposalVote(Base):
    __tablename__ = "proposal_votes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The exact proposal (thread_decision row) shown in the modal when voting.
    thread_decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("thread_decisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalized for easy analytics without a join back to thread_decisions.
    thread_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    agent_a: Mapped[str | None] = mapped_column(String(50), nullable=True)
    agent_b: Mapped[str | None] = mapped_column(String(50), nullable=True)

    vote: Mapped[str] = mapped_column(String(8), nullable=False)  # VOTE_UP / VOTE_DOWN
    details: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Random client-generated id (browser localStorage). Lets the same visitor
    # update their vote / add details, and lightly dedups without a login.
    voter_token: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
        nullable=False,
    )

    thread_decision = relationship("ThreadDecision")

    # One vote per visitor per proposal (NULL token rows are unconstrained,
    # since multiple anonymous votes without a token can't be distinguished).
    __table_args__ = (
        UniqueConstraint(
            "thread_decision_id", "voter_token", name="uq_proposal_vote_decision_voter"
        ),
    )

    def __repr__(self) -> str:
        return f"<ProposalVote {self.vote} decision={self.thread_decision_id}>"
