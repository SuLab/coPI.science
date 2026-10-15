"""help_intro_dismissals: whether a manager or reviewer answered the Help tab's prompt

Design: docs/specs/2026-10-14-help-and-onboarding-design.md §5.1.

One row per user who dismissed the first-sign-in prompt or finished a tutorial; a missing
row means "never answered". ``via`` records which. ON DELETE CASCADE removes the row with
the user. A table of its own rather than a column on ``users``, because ``User`` is mapped
by the web app, the worker and the engine, and a new mapped column breaks every image that
runs ahead of the migration.

OLD CODE ON THE NEW SCHEMA is safe: nothing old reads the table. NEW CODE ON THE OLD SCHEMA
is not: the workspace router looks the row up for every manager and reviewer GET, so those
pages 500 with UndefinedTable. Migrate before the new web image serves. The engine never
reads the table, so the agent image needs no rebuild for this revision.

The foreign key to ``users`` takes a lock on ``users`` (unlike a table with no foreign
key), so the apply waits behind any open transaction that wrote a ``users`` row, such as a
worker pipeline run; see the deploy note in docs/operations/migration-deploy-notes.md.

Downgrade drops the table and every recorded answer.

Revision ID: 0067
Revises: 0066
Create Date: 2026-10-14
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0067"
down_revision: str | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Identical to src.models.help.VIA_VALUES (tests/integration/test_migration_0067.py pins
#: it); spelled out here because a revision must not change when the model does.
VIA_VALUES = ("dismiss", "finish")


def upgrade() -> None:
    op.create_table(
        "help_intro_dismissals",
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column(
            "dismissed_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("via", sa.String(16), nullable=False),
        sa.CheckConstraint(
            "via IN (" + ", ".join(f"'{v}'" for v in VIA_VALUES) + ")",
            name="ck_help_intro_dismissals_via",
        ),
    )


def downgrade() -> None:
    op.drop_table("help_intro_dismissals")
