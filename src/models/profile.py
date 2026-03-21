"""ResearcherProfile model."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class ResearcherProfile(Base):
    __tablename__ = "researcher_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    research_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    techniques: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    experimental_models: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    disease_areas: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    key_targets: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    keywords: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    grant_titles: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    # [{label: str, content: str, submitted_at: str}]
    user_submitted_texts: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    profile_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    profile_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    raw_abstracts_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Nullable JSON: stores candidate profile awaiting user review
    pending_profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pending_profile_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    user: Mapped["User"] = relationship("User", back_populates="profile")

    def __repr__(self) -> str:
        return f"<ResearcherProfile id={self.id} user_id={self.user_id} version={self.profile_version}>"
