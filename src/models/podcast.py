"""PodcastEpisode model."""

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class PodcastEpisode(Base):
    __tablename__ = "podcast_episodes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    episode_date: Mapped[date] = mapped_column(Date, nullable=False)
    pmid: Mapped[str] = mapped_column(String(20), nullable=False)
    paper_title: Mapped[str] = mapped_column(String(500), nullable=False)
    paper_authors: Mapped[str] = mapped_column(String(500), nullable=False)
    paper_journal: Mapped[str] = mapped_column(String(255), nullable=False)
    paper_year: Mapped[int] = mapped_column(Integer, nullable=False)
    text_summary: Mapped[str] = mapped_column(Text, nullable=False)
    audio_file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    audio_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    slack_delivered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    selection_justification: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("agent_id", "episode_date", name="uq_podcast_agent_date"),
    )

    def __repr__(self) -> str:
        return f"<PodcastEpisode agent={self.agent_id} date={self.episode_date} pmid={self.pmid}>"
