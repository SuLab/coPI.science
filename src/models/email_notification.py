"""Email notification and engagement tracking models."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class EmailNotification(Base):
    __tablename__ = "email_notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    thread_decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("thread_decisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_registry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    reply_token: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(
        String(30), nullable=False, default="proposal_review"
    )  # proposal_review, new_proposal
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="sent"
    )  # sent, responded, expired
    response_type: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # review, instruction, unparseable
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])
    thread_decision: Mapped["ThreadDecision"] = relationship("ThreadDecision")
    agent: Mapped["AgentRegistry"] = relationship("AgentRegistry")

    __table_args__ = (
        # One notification per user per proposal per category
        {"comment": "unique constraint on (user_id, thread_decision_id, category) added in migration"},
    )

    def __repr__(self) -> str:
        return f"<EmailNotification user={self.user_id} category={self.category} status={self.status}>"


class EmailEngagementTracker(Base):
    __tablename__ = "email_engagement_tracking"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    consecutive_missed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    last_engagement_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_notification_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_downgrade_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])

    def __repr__(self) -> str:
        return f"<EmailEngagementTracker user={self.user_id} missed={self.consecutive_missed}>"


class EmailNotificationPreference(Base):
    """Per-user, per-category email notification preference.

    Backs the `status_overview` and `new_proposal` categories. The
    `proposal_review` category remains backed by
    User.email_notification_frequency (so the auto-downgrade ladder keeps
    mutating a single field).
    """

    __tablename__ = "email_notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    category: Mapped[str] = mapped_column(
        String(30), primary_key=True
    )  # status_overview, new_proposal
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    frequency: Mapped[str] = mapped_column(
        String(20), nullable=False, default="weekly"
    )  # daily, twice_weekly, weekly, biweekly, monthly, off
    last_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # window boundary for periodic digests (status_overview)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])

    def __repr__(self) -> str:
        return (
            f"<EmailNotificationPreference user={self.user_id} "
            f"category={self.category} enabled={self.enabled} freq={self.frequency}>"
        )
