"""Add paper_url column to podcast_episodes

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-10 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "podcast_episodes",
        sa.Column("paper_url", sa.String(1000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("podcast_episodes", "paper_url")
