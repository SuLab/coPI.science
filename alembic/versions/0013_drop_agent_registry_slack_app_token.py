"""Drop agent_registry.slack_app_token (Socket Mode never used)

Revision ID: 0013
Revises: 0012

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("agents", "slack_app_token")


def downgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("slack_app_token", sa.Text, nullable=True),
    )
