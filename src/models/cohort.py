"""Cohort models — named groups gating which agents interact during simulation.

A cohort is an admin-managed set of agents permitted to act on each other's
activity (scan, thread-activate, tag/reply). Cohorts are orthogonal to Slack
channels: channel subscriptions are unchanged; cohort membership only gates
whether one agent will *act on* another agent's posts.

The gate is an agent-behaviour filter, NOT access control: it never changes what a
human can read. PI- and admin-facing views read AgentMessage directly and stay
ungated. See .notes/cohort-system-v2.md §6.2.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func
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


# Audit action vocabulary. Kept as module constants so the routes, the engine and
# the tests cannot drift on spelling.
COHORT_ACTION_CREATED = "created"
COHORT_ACTION_DELETED = "deleted"
COHORT_ACTION_AGENT_ADDED = "agent_added"
COHORT_ACTION_AGENT_REMOVED = "agent_removed"
COHORT_ACTION_TOPOLOGY_SNAPSHOT = "topology_snapshot"

# Sentinel cohort_name for events that describe the whole topology rather than one
# cohort (run-start snapshots, bulk matrix saves). cohort_name is NOT NULL so the
# trail stays readable after a cohort is deleted.
COHORT_NAME_ALL = "*"


class CohortAuditEvent(Base):
    """Append-only audit trail for cohort mutations and topology snapshots.

    Deliberately denormalised. A cohort delete cascades its memberships away and a
    user delete nulls ``actor_id``, so the trail must not depend on either row
    surviving — hence ``cohort_name`` / ``actor_email`` and no FK on ``cohort_id``.

    ``topology`` carries the full cohort->members map plus the active gate settings,
    written at run start and on every membership change, so a finished simulation
    run stays attributable to the configuration that produced it.
    See .notes/cohort-system-v2.md §13.1.
    """

    __tablename__ = "cohort_audit_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # No FK: the row must outlive the cohort it describes.
    cohort_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    cohort_name: Mapped[str] = mapped_column(String(48), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    simulation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    topology: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<CohortAuditEvent {self.action} cohort={self.cohort_name!r} "
            f"agent={self.agent_id!r}>"
        )
