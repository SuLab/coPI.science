"""GrantBot already-posted FOA tracking.

Moved from data/grantbot_posted.json to Postgres so multiple GrantBot
instances (or a restart) cannot re-post the same FOA. The `foa_number`
PK plus `INSERT ... ON CONFLICT DO NOTHING` is the coordination primitive
that prevents duplicates even when two schedulers race.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class GrantbotPostedFoa(Base):
    __tablename__ = "grantbot_posted_foas"

    foa_number: Mapped[str] = mapped_column(String(50), primary_key=True)
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    channel: Mapped[str | None] = mapped_column(String(100), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<GrantbotPostedFoa {self.foa_number} ch={self.channel}>"
