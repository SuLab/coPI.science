"""slack_app_provisions records who started the install

One additive nullable column:

  initiated_by_user_id : UUID FK users(id) ON DELETE SET NULL — the staff
                         account that clicked "Install Slack bot".

The Slack OAuth callback is a third-party redirect: no CSRF token is possible
on it, and as of F2 (2026-09-10) its gate is staff-wide rather than admin-only.
This column is what lets ``complete_provisioning`` refuse to land a bot token
for an install a DIFFERENT account started, and what lets the callback send a
manager back to /manager/pis instead of /admin/agents.

Additive and nullable, so OLD CODE AGAINST THE NEW SCHEMA IS SAFE. NULL on
every pre-0046 row and never backfilled: those bridge rows are short-lived
(deleted on a successful install or a failed exchange) and no record of who
clicked exists for them. ``complete_provisioning`` reads NULL as "unknown
initiator — allow", which is exactly the pre-0046 behaviour.

Revision ID: 0046
Revises: 0045
Create Date: 2026-09-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0046"
down_revision: Union[str, None] = "0045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "slack_app_provisions",
        sa.Column("initiated_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "slack_app_provisions_initiated_by_user_id_fkey",
        "slack_app_provisions",
        "users",
        ["initiated_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "slack_app_provisions_initiated_by_user_id_fkey",
        "slack_app_provisions",
        type_="foreignkey",
    )
    op.drop_column("slack_app_provisions", "initiated_by_user_id")
