"""PodcastEpisode model.

Episodes are keyed by either agent_id (pilot-lab agents) or user_id (plain
ORCID users).  Exactly one should be set per row.

Uniqueness constraints:
  - uq_podcast_agent_date: one episode per agent per day (agent path)
  - ix_podcast_episodes_user_date: partial unique index (user path, via migration 0013)
"""

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class PodcastEpisode(Base):
    __tablename__ = "podcast_episodes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # For pilot-lab agents (legacy path) — nullable to support user-only episodes
    agent_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    # For plain ORCID users (no agent required)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    episode_date: Mapped[date] = mapped_column(Date, nullable=False)
    pmid: Mapped[str] = mapped_column(String(100), nullable=False)
    paper_title: Mapped[str] = mapped_column(String(500), nullable=False)
    paper_authors: Mapped[str] = mapped_column(String(500), nullable=False)
    paper_journal: Mapped[str] = mapped_column(String(255), nullable=False)
    paper_year: Mapped[int] = mapped_column(Integer, nullable=False)
    paper_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    text_summary: Mapped[str] = mapped_column(Text, nullable=False)
    audio_file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    audio_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    slack_delivered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    selection_justification: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Agent-path uniqueness (PostgreSQL ignores NULLs in UNIQUE constraints,
        # so this only enforces uniqueness when agent_id IS NOT NULL)
        UniqueConstraint("agent_id", "episode_date", name="uq_podcast_agent_date"),
        # User-path uniqueness is enforced by the partial index created in migration 0013
    )

    def __repr__(self) -> str:
        key = f"agent={self.agent_id}" if self.agent_id else f"user={self.user_id}"
        return f"<PodcastEpisode {key} date={self.episode_date} pmid={self.pmid}>"
