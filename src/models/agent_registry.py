"""Agent registry and proposal review models."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, SmallInteger, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class AgentRegistry(Base):
    __tablename__ = "agents"

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
    )  # pending, active, suspended
    slack_bot_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    slack_app_token: Mapped[str | None] = mapped_column(Text, nullable=True)
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
        "AgentDelegate", back_populates="agent", cascade="all, delete-orphan"
    )
    invitations: Mapped[list["DelegateInvitation"]] = relationship(
        "DelegateInvitation",
        foreign_keys="DelegateInvitation.agent_registry_id",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<AgentRegistry agent_id={self.agent_id} status={self.status}>"


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
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    thread_decision: Mapped["ThreadDecision"] = relationship("ThreadDecision")

    __table_args__ = (
        # Each agent can only review a thread decision once
        {"comment": "unique constraint on (thread_decision_id, agent_id) added in migration"},
    )

    def __repr__(self) -> str:
        return f"<ProposalReview agent={self.agent_id} rating={self.rating}>"
