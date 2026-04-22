"""MatchmakerProposal model.

Admin-generated collaboration proposals produced from public + private profiles
without requiring an agent simulation run.
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
    pi_a_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pi_b_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    proposal_md: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    confidence: Mapped[str] = mapped_column(String(20), nullable=False)  # high / moderate / speculative
    llm_model: Mapped[str] = mapped_column(String(100), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    pi_a: Mapped["User"] = relationship("User", foreign_keys=[pi_a_id])
    pi_b: Mapped["User"] = relationship("User", foreign_keys=[pi_b_id])

    def __repr__(self) -> str:
        return f"<MatchmakerProposal pi_a={self.pi_a_id} pi_b={self.pi_b_id} confidence={self.confidence}>"
