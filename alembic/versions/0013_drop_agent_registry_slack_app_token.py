"""Drop agent_registry.slack_app_token (Socket Mode never used)

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-30 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("agent_registry", "slack_app_token")


def downgrade() -> None:
    op.add_column(
        "agent_registry",
        sa.Column("slack_app_token", sa.Text, nullable=True),
    )
