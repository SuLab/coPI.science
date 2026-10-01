"""A DB copy of every rubric document an engine loaded (AP-7).

Write-only in this program (spec §9.5, FA2-V4): `rubric_revisions` keeps resolving
provenance from the live rubric, then `revisions.toml`, then unknown. Nothing reads
this table yet; it exists so a future resolver has the exact bytes each stamp names.
`content_hash` is the same `sha256[:12]` stamp rows carry
(`blackbird_rubric.Rubric.content_hash`)."""
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class RubricDocument(Base):
    __tablename__ = "rubric_documents"

    content_hash: Mapped[str] = mapped_column(String(20), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    toml: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
