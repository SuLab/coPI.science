"""Add opportunity_assessments.panel_incomplete / .missing_domains

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-18 00:00:00.000000

Additive with a server default, so old code against the new schema keeps
working. The reverse is NOT safe: the model maps both columns as of this
change, so every select(OpportunityAssessment) names them and would raise
UndefinedColumn against a pre-0029 database. Migrate BEFORE the new code
serves — the same ordering 0028 needed.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "opportunity_assessments",
        sa.Column(
            "panel_incomplete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "opportunity_assessments",
        sa.Column("missing_domains", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("opportunity_assessments", "missing_domains")
    op.drop_column("opportunity_assessments", "panel_incomplete")
