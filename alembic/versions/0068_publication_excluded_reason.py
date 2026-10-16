"""publications.excluded_reason: why a row is excluded (publication handling)

Design: the 2026-10-15 publication-handling design, contract C3 and R4.1.

One nullable VARCHAR(32) column plus the CHECK ``ck_publications_excluded_reason``, which
admits NULL or one of seven reasons. Every writer sets the reason together with
``excluded_at`` and Restore clears both. NULL means the row is in use, or was excluded
before this revision (never backfilled). The CHECK does not pair the reason with
``excluded_at``: a pairing CHECK would make old code's Restore, which clears only
``excluded_at``, fail after a rollback.

OLD CODE ON THE NEW SCHEMA is safe: it never reads or writes the column, and the CHECK admits
NULL. NEW CODE ON THE OLD SCHEMA is not: ``Publication`` maps the column, so every
publication read on the web app and the worker raises UndefinedColumn. Migrate before the
new app and worker serve. The engine never queries ``publications``, so the agent image
needs no rebuild for this revision.

Adding the CHECK validates it by scanning ``publications`` under ACCESS EXCLUSIVE, so the
worker must be idle (no ``processing`` job) or stopped; see the deploy note in
docs/operations/migration-deploy-notes.md.

Downgrade drops the constraint, then the column, and so discards every recorded reason;
Restore then falls back to the ``excluded_by_user_id`` rule. A row restored by old code after
a rollback can keep a stale reason with ``excluded_at`` NULL; readers key on ``excluded_at``
only.

Revision ID: 0068
Revises: 0067
Create Date: 2026-10-15
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Identical to src.models.publication.EXCLUDED_REASON_CHECK_SQL
#: (tests/integration/test_migration_0068.py pins it); spelled out here because a revision
#: must not change when the model does.
EXCLUDED_REASON_CHECK_SQL = (
    "excluded_reason IS NULL OR excluded_reason IN ('staff', 'superseded', 'pubmed_twin', "
    "'duplicate_title', 'publication_type', 'openalex_affiliation', 'repair_script')"
)


def upgrade() -> None:
    op.add_column("publications", sa.Column("excluded_reason", sa.String(32), nullable=True))
    op.create_check_constraint(
        "ck_publications_excluded_reason", "publications", EXCLUDED_REASON_CHECK_SQL
    )


def downgrade() -> None:
    op.drop_constraint("ck_publications_excluded_reason", "publications", type_="check")
    op.drop_column("publications", "excluded_reason")
