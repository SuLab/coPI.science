"""Add users.user_role (PI / manager / admin account types)

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-17 00:00:00.000000

Additive on purpose. The model stops mapping users.is_admin in the same change,
so this migration is safe to apply BEFORE the new code is running and the old
code keeps working after it — there is no window where live code and applied
schema disagree. The column drop is deferred to 0029, a separate later deploy.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("user_role", sa.String(20), nullable=False, server_default="pi"),
    )
    op.execute("UPDATE users SET user_role = 'admin' WHERE is_admin = true")
    # 0001_initial.py:33 declared is_admin with Alembic's Python-side `default=`,
    # which emits NO DDL DEFAULT — so the column is NOT NULL with nothing to
    # fall back on. The model stops mapping it as of this change, so without
    # this line the next INSERT INTO users omits the column, Postgres rejects
    # the row, and src/routers/auth.py:215 can no longer create a user. See F14.
    op.alter_column("users", "is_admin", server_default=sa.text("false"))
    op.create_check_constraint(
        "ck_users_user_role",
        "users",
        "user_role IN ('pi', 'manager', 'admin')",
    )


def downgrade() -> None:
    # Restore is_admin from the enum before dropping the enum, so the downgrade
    # is data-preserving rather than just structurally reversible.
    op.execute("UPDATE users SET is_admin = (user_role = 'admin')")
    op.drop_constraint("ck_users_user_role", "users", type_="check")
    op.drop_column("users", "user_role")
    op.alter_column("users", "is_admin", server_default=None)
