"""Add web delegate tables and proposal review audit column

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-03 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # DelegateInvitation table
    op.create_table(
        "delegate_invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agent_registry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "invited_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("token", sa.String(64), unique=True, nullable=False, index=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column(
            "accepted_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )

    # Partial unique index: one pending invitation per email per agent
    op.create_index(
        "ix_delegate_invitations_pending_unique",
        "delegate_invitations",
        ["agent_registry_id", "email"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    # AgentDelegate table
    op.create_table(
        "agent_delegates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agent_registry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "invitation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("delegate_invitations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("notify_proposals", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("agent_registry_id", "user_id", name="uq_agent_delegate_agent_user"),
    )

    # Add delegate_user_id audit column to proposal_reviews
    op.add_column(
        "proposal_reviews",
        sa.Column(
            "delegate_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("proposal_reviews", "delegate_user_id")
    op.drop_table("agent_delegates")
    op.drop_index("ix_delegate_invitations_pending_unique", table_name="delegate_invitations")
    op.drop_table("delegate_invitations")
