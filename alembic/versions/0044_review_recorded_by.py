"""review rows record who physically entered them when impersonating

Two additive nullable columns, one per table:

  assessment_reviews       : recorded_by_user_id
  assessment_review_events : recorded_by_user_id

Additive and nullable, so OLD CODE AGAINST THE NEW SCHEMA IS SAFE. NULL means
the user named in reviewer_user_id/actor_user_id acted in person; a non-NULL
value records the admin who entered the row while impersonating that user
(operator decision 2026-09-10, reversing N1/A15). FK to users, ON DELETE SET
NULL, the same reviewer/actor-survives-deletion pattern the rest of this
table set already follows.

Revision ID: 0044
Revises: 0043
Create Date: 2026-09-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("assessment_reviews", "assessment_review_events"):
        op.add_column(
            table,
            sa.Column(
                "recorded_by_user_id", postgresql.UUID(as_uuid=True), nullable=True
            ),
        )
        op.create_foreign_key(
            f"fk_{table}_recorded_by_user_id_users",
            table,
            "users",
            ["recorded_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    for table in ("assessment_review_events", "assessment_reviews"):
        op.drop_constraint(
            f"fk_{table}_recorded_by_user_id_users", table, type_="foreignkey"
        )
        op.drop_column(table, "recorded_by_user_id")
