"""Access gate + waitlist

Revision ID: 0010a
Revises: 0010
Create Date: 2026-04-15 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010a"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Seeded allowlist — pilot lab PIs. Matches orcids.txt at migration time.
PILOT_ORCIDS = [
    ("0000-0002-9859-4104", "Andrew Su"),
    ("0000-0001-9287-6840", "Luke Wiseman"),
    ("0000-0002-6299-8799", "Martin Lotz"),
    ("0000-0001-5330-3492", "Benjamin Cravatt"),
    ("0000-0001-5908-7882", "Danielle Grotjahn"),
    ("0000-0002-1010-145X", "Michael Petrascheck"),
    ("0000-0001-8336-9935", "Megan Ken"),
    ("0000-0003-2209-7301", "Lisa Racki"),
    ("0000-0001-5718-5542", "Enrique Saez"),
    ("0000-0002-2629-6124", "Chunlei Wu"),
    ("0000-0001-7153-3769", "Andrew Ward"),
    ("0000-0001-9535-2866", "Bryan Briney"),
    ("0000-0002-5964-7111", "Stefano Forli"),
    ("0000-0003-2819-4049", "Ashok Deniz"),
    ("0000-0001-6701-996X", "Luke Lairson"),
]


def upgrade() -> None:
    # 1. Add access_status to users, default 'pending', backfill existing rows to 'allowed'
    op.add_column(
        "users",
        sa.Column("access_status", sa.String(20), nullable=False, server_default="pending"),
    )
    op.execute("UPDATE users SET access_status = 'allowed'")
    # Drop the server default so new inserts rely on the model default
    op.alter_column("users", "access_status", server_default=None)

    # 2. Access allowlist table
    op.create_table(
        "access_allowlist",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("orcid", sa.String(50), nullable=False, unique=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "added_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # 3. Waitlist signups
    op.create_table(
        "waitlist_signups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("institution", sa.String(255), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("contacted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # 4. Seed pilot-lab ORCIDs onto the allowlist
    allowlist = sa.table(
        "access_allowlist",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("orcid", sa.String),
        sa.column("note", sa.Text),
    )
    import uuid
    op.bulk_insert(
        allowlist,
        [
            {"id": uuid.uuid4(), "orcid": orcid, "note": f"Pilot lab: {name}"}
            for orcid, name in PILOT_ORCIDS
        ],
    )


def downgrade() -> None:
    op.drop_table("waitlist_signups")
    op.drop_table("access_allowlist")
    op.drop_column("users", "access_status")
