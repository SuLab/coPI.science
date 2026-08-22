"""Add agents.muted_at / agents.muted_by — mute is a purpose-built control
over the existing 'inactive' status, not a new status value (design decision
D2/D3). Both nullable and additive.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-21 00:00:00.000000

Migrate-before-serve applies, same one-way constraint as 0028/0030/0032: once
AgentRegistry maps these columns, every existing select(AgentRegistry) in the
app — including src/routers/manager.py's _manager_set_mute helper
(`select(AgentRegistry).where(AgentRegistry.user_id == user_id)`), the very
query this mute/unmute feature runs — names them in its column list, and
against a pre-migration database that raises UndefinedColumn. Old code
against the new schema is safe (nullable, no backfill).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agents", sa.Column("muted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "agents",
        sa.Column("muted_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_agents_muted_by_users",
        "agents", "users",
        ["muted_by"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_agents_muted_by", "agents", ["muted_by"])


def downgrade() -> None:
    op.drop_index("ix_agents_muted_by", table_name="agents")
    op.drop_constraint("fk_agents_muted_by_users", "agents", type_="foreignkey")
    op.drop_column("agents", "muted_by")
    op.drop_column("agents", "muted_at")
