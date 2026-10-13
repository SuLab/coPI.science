"""user_microsoft_identities: a staff account's linked Microsoft sign-in

Design: docs/specs/2026-10-13-microsoft-staff-sign-in-design.md §5.1.

One row per linked account: Microsoft's immutable (tid, oid) pair, unique, and the user it signs
in to, unique (one link per user). ON DELETE CASCADE removes it with the user.

OLD CODE ON THE NEW SCHEMA is safe: nothing old reads the table. NEW CODE ON THE OLD SCHEMA is
not: the Settings page and every Microsoft route query it, so migrate before the new web image
serves. Downgrade drops the table and every link in it.

Revision ID: 0066
Revises: 0065
Create Date: 2026-10-13
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_microsoft_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sign_in_name", sa.String(255), nullable=True),
        sa.Column(
            "linked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_sign_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", name="uq_user_microsoft_identities_user_id"),
        sa.UniqueConstraint(
            "tenant_id", "object_id", name="uq_user_microsoft_identities_tenant_object"
        ),
    )


def downgrade() -> None:
    op.drop_table("user_microsoft_identities")
