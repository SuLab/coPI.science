"""Email verification and server-side session revocation (docs/specs/2026-10-01-web-ui-remediation-design.md §6.6, §6.7)

Two additive nullable columns on users:

- users.email_verified_at: when an administrator (any user) or a manager (PIs)
  verified the address in users.email. Cleared by src/services/user_email.py
  whenever the address changes; delegate-invitation acceptance requires it.
- users.session_epoch: bumped by logout, access denial and a role change; a
  session whose stored epoch differs is refused (src/dependencies.py). NULL
  counts as 0. Never backfilled.

BACKFILL, BY OWNER DECISION D8 — an explicit override of this repo's rule that
NULL means "never asked" and is not backfilled: every user that has an email
when this runs is marked verified (email_verified_at = now()).

OLD CODE ON THE NEW SCHEMA is safe: nothing old reads either column (an address
changed through old code after this runs keeps its stamp, so keep the window
between --apply and the new web image short). NEW CODE ON THE OLD SCHEMA is not:
User maps both columns, so every select of User raises UndefinedColumn. Migrate
BEFORE the new code serves, with the WORKER IDLE: ALTER TABLE users queues behind
any open transaction that has read users, and the worker holds its transaction
across a whole pipeline run, past the chain's 10 s lock_timeout.

Downgrade drops both columns, and with them every verification stamp.

Revision ID: 0057
Revises: 0056
Create Date: 2026-10-01
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("users", sa.Column("session_epoch", sa.Integer(), nullable=True))
    # D8 (owner override, see the module docstring).
    op.execute("UPDATE users SET email_verified_at = now() WHERE email IS NOT NULL")


def downgrade() -> None:
    op.drop_column("users", "session_epoch")
    op.drop_column("users", "email_verified_at")
