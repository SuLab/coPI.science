"""Grantbot posted FOAs table (replaces data/grantbot_posted.json)

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-24 00:00:00.000000

"""

import json
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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

    # Backfill from legacy JSON file if present.
    legacy_path = Path("data/grantbot_posted.json")
    if legacy_path.exists():
        try:
            data = json.loads(legacy_path.read_text(encoding="utf-8"))
            numbers = data.get("posted", []) if isinstance(data, dict) else []
        except Exception:
            numbers = []
        if numbers:
            bind = op.get_bind()
            bind.execute(
                sa.text(
                    "INSERT INTO grantbot_posted_foas (foa_number) "
                    "VALUES (:n) ON CONFLICT (foa_number) DO NOTHING"
                ),
                [{"n": n} for n in numbers if n],
            )


def downgrade() -> None:
    op.drop_table("grantbot_posted_foas")
