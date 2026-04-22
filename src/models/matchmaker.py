"""MatchmakerProposal model.

Proposals can be created two ways:
  1. Admin web UI — pi_a_id / pi_b_id are set (FK → users); pi_a_name / pi_b_name left null.
  2. CLI script    — pi_a_name / pi_b_name are set (from profiles/ filenames); FKs left null.

Templates use pi_a.name if the FK is populated, otherwise fall back to pi_a_name.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class MatchmakerProposal(Base):
    __tablename__ = "matchmaker_proposals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Web-UI path: FK to users table
    pi_a_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    pi_b_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # CLI path: display name from profile filename / header
    pi_a_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pi_b_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    proposal_md: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    confidence: Mapped[str] = mapped_column(String(20), nullable=False)  # high / moderate / speculative
    llm_model: Mapped[str] = mapped_column(String(100), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    pi_a: Mapped["User | None"] = relationship("User", foreign_keys=[pi_a_id])
    pi_b: Mapped["User | None"] = relationship("User", foreign_keys=[pi_b_id])

    @property
    def name_a(self) -> str:
        return self.pi_a.name if self.pi_a else (self.pi_a_name or "Unknown")

    @property
    def name_b(self) -> str:
        return self.pi_b.name if self.pi_b else (self.pi_b_name or "Unknown")

    def __repr__(self) -> str:
        return f"<MatchmakerProposal {self.name_a!r} × {self.name_b!r} confidence={self.confidence}>"
