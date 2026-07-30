"""Cohort models — named groups gating which agents interact during simulation.

A cohort is an admin-managed set of agents permitted to act on each other's
activity (scan, thread-activate, tag/reply). Cohorts are orthogonal to Slack
channels: channel subscriptions are unchanged; cohort membership only gates
whether one agent will *act on* another agent's posts. See specs/cohort-system.md.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class Cohort(Base):
    __tablename__ = "cohorts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    memberships: Mapped[list["CohortMembership"]] = relationship(
        "CohortMembership", back_populates="cohort", cascade="all, delete-orphan"
    )
    created_by_user: Mapped["User | None"] = relationship(
        "User", foreign_keys=[created_by]
    )

    def __repr__(self) -> str:
        return f"<Cohort name={self.name!r}>"


class CohortMembership(Base):
    __tablename__ = "cohort_memberships"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    cohort_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cohorts.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Matches AgentRegistry.agent_id (slug). No FK: agent rows may not exist at
    # membership-creation time; the application validates at add time.
    agent_id: Mapped[str] = mapped_column(String(50), nullable=False)
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    cohort: Mapped["Cohort"] = relationship("Cohort", back_populates="memberships")
    added_by_user: Mapped["User | None"] = relationship(
        "User", foreign_keys=[added_by]
    )

    __table_args__ = (
        # One membership row per (cohort, agent)
        {"comment": "unique constraint on (cohort_id, agent_id) added in migration"},
    )

    def __repr__(self) -> str:
        return f"<CohortMembership cohort={self.cohort_id} agent={self.agent_id}>"
