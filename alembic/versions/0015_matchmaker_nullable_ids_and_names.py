"""Make matchmaker PI FKs nullable; add pi_a_name / pi_b_name for CLI path

Revision ID: 0015
Revises: 0014
Create Date: 2026-04-22 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("matchmaker_proposals", "pi_a_id", nullable=True)
    op.alter_column("matchmaker_proposals", "pi_b_id", nullable=True)
    op.add_column("matchmaker_proposals", sa.Column("pi_a_name", sa.String(255), nullable=True))
    op.add_column("matchmaker_proposals", sa.Column("pi_b_name", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("matchmaker_proposals", "pi_b_name")
    op.drop_column("matchmaker_proposals", "pi_a_name")
    op.alter_column("matchmaker_proposals", "pi_b_id", nullable=False)
    op.alter_column("matchmaker_proposals", "pi_a_id", nullable=False)
