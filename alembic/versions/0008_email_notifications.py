"""Add email notification tables and columns

Revision ID: 0008
Revises: 0007

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # New columns on users
    op.add_column(
        "users",
        sa.Column(
            "email_notification_frequency",
            sa.String(20),
            nullable=False,
            server_default="weekly",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "email_notifications_paused_by_system",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # New columns on proposal_reviews
    op.add_column(
        "proposal_reviews",
        sa.Column(
            "reviewed_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "proposal_reviews",
        sa.Column(
            "submitted_via",
            sa.String(10),
            nullable=False,
            server_default="web",
        ),
    )

    # EmailNotification table
    op.create_table(
        "email_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "thread_decision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("thread_decisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_registry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "reply_token", sa.String(64), unique=True, nullable=False, index=True
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="sent"),
        sa.Column("response_type", sa.String(20), nullable=True),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "user_id",
            "thread_decision_id",
            name="uq_email_notification_user_thread",
        ),
    )

    # EmailEngagementTracker table
    op.create_table(
        "email_engagement_tracking",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "consecutive_missed", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("last_engagement_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_notification_sent_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("last_downgrade_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("email_engagement_tracking")
    op.drop_table("email_notifications")
    op.drop_column("proposal_reviews", "submitted_via")
    op.drop_column("proposal_reviews", "reviewed_by_user_id")
    op.drop_column("users", "email_notifications_paused_by_system")
    op.drop_column("users", "email_notification_frequency")
