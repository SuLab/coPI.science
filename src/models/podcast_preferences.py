"""PodcastPreferences model — per-agent or per-user podcast customization.

Rows are keyed by either agent_id (for approved pilot-lab agents) or user_id
(for any user who has completed ORCID onboarding).  Exactly one of the two
should be set on each row; both being set is invalid.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class PodcastPreferences(Base):
    __tablename__ = "podcast_preferences"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # For pilot-lab agents (legacy path)
    agent_id: Mapped[str | None] = mapped_column(String(50), nullable=True, unique=True, index=True)
    # For plain ORCID users (no agent required)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        unique=True,
        index=True,
    )
    voice_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    extra_keywords: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default="{}"
    )
    preferred_journals: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default="{}"
    )
    deprioritized_journals: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default="{}"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        key = f"agent={self.agent_id}" if self.agent_id else f"user={self.user_id}"
        return f"<PodcastPreferences {key} voice={self.voice_id}>"
