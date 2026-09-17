"""Cascade-delete private_channel_members rows when their user is deleted

Revision ID: 0026
Revises: 0025

private_channel_members.user_id was ondelete="SET NULL" (0011). A role="pi"
membership row always has agent_id IS NULL (src/services/private_channels.py
never sets agent_id on a PI row), so a SET NULL on user_id collides with the
pcm_exactly_one_of_agent_or_user CHECK — the row would end up with BOTH
columns NULL, which is neither "bot" nor "pi". The DB raises a
CheckViolationError and the whole DELETE FROM users fails, making any PI who
was ever a private-channel member permanently undeletable (live-reproduced;
tests/integration/test_db_contract.py now asserts the delete succeeds).

CASCADE is a pure behaviour change on an existing column, not a backfill:
every existing PI membership row already satisfies the CHECK (agent_id IS
NULL, user_id IS NOT NULL), so there is no data pre-step. added_by_user_id
stays ondelete="SET NULL" — nulling it never violates the CHECK (see the
test_deleting_added_by_user_is_safe contrast case).

The FK is dropped and recreated under its ORIGINAL implicit name
(private_channel_members_user_id_fkey — 0011's op.create_table gave it no
explicit name, so Postgres assigned the default `<table>_<column>_fkey`)
so the two directions of this migration are exact inverses and no other
tooling needs to learn a new constraint name.

The constraint to drop is not assumed to be named `_FK` — a
`Base.metadata.create_all` bootstrap, a pg_dump/restore that renamed it, or a
hand-edit could leave prod's actual name different, and a hard-coded
`op.drop_constraint` with no `if_exists` would abort `alembic upgrade` mid
stop-the-world window with `constraint "..." does not exist`. Both upgrade()
and downgrade() resolve the real name from `pg_constraint` first (the single
foreign key on `private_channel_members.user_id`) and only fall back to `_FK`
as the expected-default label in the error message / when downgrade finds none
(if_exists-safe no-op). Either direction always recreates the FK under the
canonical `_FK` name, so the two directions stay exact inverses regardless of
what name upgrade() found on the way in.

Downgrade is idempotent (if_exists) per the branch convention (0022+).
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FK = "private_channel_members_user_id_fkey"


def _resolve_user_id_fk(conn) -> str | None:
    """The actual name of the (single-column) FK on private_channel_members.user_id,
    or None if there isn't one."""
    return conn.execute(
        sa.text(
            """
            SELECT con.conname
              FROM pg_constraint con
              JOIN pg_attribute att
                ON att.attrelid = con.conrelid AND att.attnum = ANY(con.conkey)
             WHERE con.conrelid = 'private_channel_members'::regclass
               AND con.contype = 'f'
               AND att.attname = 'user_id'
               AND cardinality(con.conkey) = 1
            """
        )
    ).scalar_one_or_none()


def upgrade() -> None:
    conn = op.get_bind()
    fk_name = _resolve_user_id_fk(conn)
    if fk_name is None:
        raise RuntimeError(
            "No single-column foreign key found on private_channel_members.user_id "
            f"(expected {_FK!r} by default). Cannot add the CASCADE behaviour without "
            "knowing which constraint to drop and recreate — verify the column's "
            "actual FK name on this database before re-running this migration."
        )
    op.drop_constraint(fk_name, "private_channel_members", type_="foreignkey")
    op.create_foreign_key(
        _FK, "private_channel_members", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )


def downgrade() -> None:
    conn = op.get_bind()
    fk_name = _resolve_user_id_fk(conn) or _FK
    op.drop_constraint(fk_name, "private_channel_members", type_="foreignkey", if_exists=True)
    op.create_foreign_key(
        _FK, "private_channel_members", "users", ["user_id"], ["id"], ondelete="SET NULL"
    )
