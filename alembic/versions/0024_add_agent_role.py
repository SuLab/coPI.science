"""Add role column to agents (per-role agent customization)

Revision ID: 0024
Revises: 0023

`role` selects per-role prompt overrides (prompts/roles/{role}/) and a per-role
tool allow-list. Default 'pi_lab' == the pre-existing all-agents-identical
behaviour, so this column is a no-op until an agent is explicitly reassigned.

Downgrade is idempotent (if_exists) per the branch convention (0022/0023).
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("role", sa.String(length=20), nullable=False, server_default="pi_lab"),
    )


def downgrade() -> None:
    op.drop_column("agents", "role", if_exists=True)
