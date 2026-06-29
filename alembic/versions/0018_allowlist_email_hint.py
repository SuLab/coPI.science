"""Add email hint column to access_allowlist (fallback for private ORCID emails)

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-29 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Fallback email used at registration when the ORCID public API exposes no
    # email (private by default). Resolver reuses the allowlist row already
    # fetched at login, so this adds no extra query.
    op.add_column(
        "access_allowlist",
        sa.Column("email", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("access_allowlist", "email")
