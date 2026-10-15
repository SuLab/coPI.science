"""Whether a manager or reviewer has answered the Help tab's first-sign-in prompt
(migration 0067; spec docs/specs/2026-10-14-help-and-onboarding-design.md §5.1).

One row per user who dismissed the prompt or finished a tutorial; no row means never
answered. No relationship is declared on User: deleting the user removes the row through
the database's ON DELETE CASCADE, and a relationship without passive_deletes would make
db.delete(user) try to null this primary-key foreign key.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

VIA_DISMISS = "dismiss"
VIA_FINISH = "finish"
VIA_VALUES = (VIA_DISMISS, VIA_FINISH)
CHECK_NAME = "ck_help_intro_dismissals_via"


class HelpIntroDismissal(Base):
    __tablename__ = "help_intro_dismissals"
    __table_args__ = (
        CheckConstraint("via IN ('dismiss', 'finish')", name=CHECK_NAME),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    via: Mapped[str] = mapped_column(String(16), nullable=False)
