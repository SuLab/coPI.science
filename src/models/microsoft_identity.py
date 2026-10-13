"""A staff account's linked Microsoft (Entra ID) sign-in (migration 0066; spec
docs/specs/2026-10-13-microsoft-staff-sign-in-design.md §5.1).

Looked up by (tenant_id, object_id) only: Microsoft's `tid` and `oid`, the immutable pair it
says to key application data on. `sign_in_name` (`preferred_username`) is display-only. No
relationship is declared on User: deleting the user removes the row through the database's
ON DELETE CASCADE, so delete_user_account never has to load it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class UserMicrosoftIdentity(Base):
    __tablename__ = "user_microsoft_identities"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_microsoft_identities_user_id"),
        UniqueConstraint(
            "tenant_id", "object_id", name="uq_user_microsoft_identities_tenant_object"
        ),
    )
    #: The INSERT returns `linked_at` itself: an expired server default would lazy-load on
    #: first read, which an async session cannot do.
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sign_in_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_sign_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
