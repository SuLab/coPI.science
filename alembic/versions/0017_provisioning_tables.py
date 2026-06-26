"""Add app_settings (KV) + slack_app_provisions tables for self-service provisioning

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-26 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Generic durable KV store (Slack config token + refresh token live here,
    # since Slack rotates them on every use and lru_cached Settings/.env can't
    # be written from a request handler).
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=100), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Short-lived bridge between the admin "Provision" click and the Slack OAuth
    # callback (holds the app client_secret + a random state).
    op.create_table(
        "slack_app_provisions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agent_registry_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=64), nullable=False, unique=True),
        sa.Column("client_id", sa.String(length=100), nullable=False),
        sa.Column("client_secret", sa.Text(), nullable=False),
        sa.Column("app_id", sa.String(length=50), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("slack_app_provisions")
    op.drop_table("app_settings")
