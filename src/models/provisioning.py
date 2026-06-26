"""Models backing self-service Slack provisioning and a generic app KV store."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class AppSetting(Base):
    """Generic key/value store for durable runtime config.

    Used for the Slack *app-configuration* token + refresh token, which Slack
    rotates on every use: the rotated pair must be persisted, and the
    ``@lru_cache``d ``Settings`` / ``.env`` cannot be written from a request
    handler. Read-through to ``Settings`` seeds the first value; thereafter the
    KV row is authoritative.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<AppSetting key={self.key}>"


class SlackAppProvision(Base):
    """Short-lived bridge between the admin "Provision" click and the Slack OAuth
    callback.

    The callback needs the app's ``client_secret`` to exchange the temporary
    code, and a random unguessable ``state`` to validate Slack's redirect (no
    CSRF token is possible on a third-party redirect). Rows are deleted on a
    successful install.
    """

    __tablename__ = "slack_app_provisions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_registry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    client_id: Mapped[str] = mapped_column(String(100), nullable=False)
    client_secret: Mapped[str] = mapped_column(Text, nullable=False)
    app_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<SlackAppProvision agent={self.agent_registry_id} state={self.state[:8]}…>"
