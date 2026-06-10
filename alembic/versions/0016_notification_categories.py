"""Add notification categories: email_notification_preferences + category column

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-10 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Per-user, per-category preference table (status_overview, new_proposal).
    op.create_table(
        "email_notification_preferences",
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("category", sa.String(length=30), primary_key=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "frequency", sa.String(length=20), nullable=False, server_default="weekly"
        ),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Category column on email_notifications so the table can log all kinds.
    op.add_column(
        "email_notifications",
        sa.Column(
            "category",
            sa.String(length=30),
            nullable=False,
            server_default="proposal_review",
        ),
    )

    # Widen the uniqueness to include category: a single proposal can now
    # produce both a proposal_review reminder and a new_proposal alert.
    op.drop_constraint(
        "uq_email_notification_user_thread",
        "email_notifications",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_email_notification_user_thread_category",
        "email_notifications",
        ["user_id", "thread_decision_id", "category"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_email_notification_user_thread_category",
        "email_notifications",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_email_notification_user_thread",
        "email_notifications",
        ["user_id", "thread_decision_id"],
    )
    op.drop_column("email_notifications", "category")
    op.drop_table("email_notification_preferences")
