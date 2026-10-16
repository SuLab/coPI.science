"""Profile revision owner

Design: the 2026-10-15 publication-handling design, C6 and §10 R5.17. Additive:

- ``profile_revisions.owner_user_id`` (uuid, NULL): the user who owned the agent when the
  revision was written. Foreign key ``fk_profile_revisions_owner_user_id_users`` to
  ``users.id`` ON DELETE CASCADE, and index ``ix_profile_revisions_owner_user_id``. Account
  deletion purges a user's revisions by it.

No backfill: NULL means "written before 0069" (or for an agent with no user), and account
deletion purges a NULL row by the agent the deleted user owns at deletion.

OLD CODE ON THE NEW SCHEMA is safe: its inserts leave the column NULL, which the legacy rule
purges. NEW CODE ON THE OLD SCHEMA is not: ``ProfileRevision`` maps the column, so profile
saves, the pipeline, deletion and the staff revision list raise UndefinedColumn, and the
engine's memory-revision write is swallowed to a WARNING (src/agent/engine/memory.py).
Migrate BEFORE the new images serve, with the worker idle (no ``processing`` job): the
foreign key takes a lock on ``users``, which the worker's pipeline transaction updates, and
the chain's lock_timeout is 10 s. Rebuild all three images.

Downgrade drops the index, then the column (its foreign key goes with it) and every recorded
owner.

Revision ID: 0069
Revises: 0068
Create Date: 2026-10-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "profile_revisions",
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_profile_revisions_owner_user_id_users", "profile_revisions", "users",
        ["owner_user_id"], ["id"], ondelete="CASCADE",
    )
    op.create_index(
        "ix_profile_revisions_owner_user_id", "profile_revisions", ["owner_user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_profile_revisions_owner_user_id", table_name="profile_revisions")
    op.drop_column("profile_revisions", "owner_user_id")
