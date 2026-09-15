"""Add profile_revisions table

Revision ID: 0009
Revises: 0008

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "profile_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agent_registry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("profile_type", sa.String(10), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "changed_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("mechanism", sa.String(20), nullable=False),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_profile_revision_agent_type_created",
        "profile_revisions",
        ["agent_registry_id", "profile_type", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_profile_revision_agent_type_created",
        table_name="profile_revisions",
    )
    op.drop_table("profile_revisions")
