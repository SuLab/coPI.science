"""Delegate invitation and relationship models."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class DelegateInvitation(Base):
    __tablename__ = "delegate_invitations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_registry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    invited_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )  # pending, accepted, expired, revoked
    accepted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Relationships
    agent: Mapped["AgentRegistry"] = relationship(
        "AgentRegistry", foreign_keys=[agent_registry_id], overlaps="invitations"
    )
    invited_by: Mapped["User"] = relationship("User", foreign_keys=[invited_by_user_id])

    def __repr__(self) -> str:
        return f"<DelegateInvitation email={self.email!r} status={self.status}>"


class AgentDelegate(Base):
    __tablename__ = "agent_delegates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_registry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    invitation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("delegate_invitations.id", ondelete="SET NULL"),
        nullable=True,
    )
    notify_proposals: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    # Relationships
    agent: Mapped["AgentRegistry"] = relationship("AgentRegistry", back_populates="delegates")
    user: Mapped["User"] = relationship("User", back_populates="delegated_agents")
    invitation: Mapped["DelegateInvitation | None"] = relationship("DelegateInvitation")

    __table_args__ = (
        # One delegation relationship per user per agent
        {"comment": "unique constraint on (agent_registry_id, user_id) added in migration"},
    )

    def __repr__(self) -> str:
        return f"<AgentDelegate agent={self.agent_registry_id} user={self.user_id}>"
