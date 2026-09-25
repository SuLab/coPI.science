"""Profile revision tracking model."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class ProfileRevision(Base):
    __tablename__ = "profile_revisions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_registry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    profile_type: Mapped[str] = mapped_column(
        String(10), nullable=False
    )  # public, memory; "private" only on pre-2026-08-13 rows
    content: Mapped[str] = mapped_column(Text, nullable=False)
    changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    mechanism: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # web, agent, pipeline; "slack_dm" only on pre-2026-08-13 rows.
    # "monthly_refresh" is never written: that job records "pipeline".
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    agent: Mapped["AgentRegistry"] = relationship("AgentRegistry")
    changed_by: Mapped["User | None"] = relationship("User", foreign_keys=[changed_by_user_id])

    __table_args__ = (
        Index(
            "ix_profile_revision_agent_type_created",
            "agent_registry_id",
            "profile_type",
            created_at.desc(),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<ProfileRevision agent={self.agent_registry_id} "
            f"type={self.profile_type} mechanism={self.mechanism}>"
        )
