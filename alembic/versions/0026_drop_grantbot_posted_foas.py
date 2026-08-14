"""Drop grantbot_posted_foas (GrantBot/FOA surface retired)

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-12 00:00:00.000000

GrantBot and the whole funding/FOA leaf surface are being removed (branch-2
engine reconciliation, Task 3). ``grantbot_posted_foas`` was its own dedicated
FOA-dedup coordination table (see 0012), not a column on a shared table, so
dropping it is a clean, isolated migration. No production data in this table
has any value once GrantBot itself is gone — it recorded only "which FOAs
were already posted."

Downgrade recreates the table exactly as 0012's upgrade() built it (4 columns,
no legacy-JSON backfill — that one-time seed step is not reproducible here).
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("grantbot_posted_foas")


def downgrade() -> None:
    op.create_table(
        "grantbot_posted_foas",
        sa.Column("foa_number", sa.String(50), primary_key=True),
        sa.Column(
            "posted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("channel", sa.String(100), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
    )
