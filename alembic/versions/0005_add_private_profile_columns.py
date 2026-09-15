"""Add private_profile_md and private_profile_seed columns

Revision ID: 0005
Revises: 0004

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "researcher_profiles",
        sa.Column("private_profile_md", sa.Text(), nullable=True),
    )
    op.add_column(
        "researcher_profiles",
        sa.Column("private_profile_seed", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("researcher_profiles", "private_profile_seed")
    op.drop_column("researcher_profiles", "private_profile_md")
