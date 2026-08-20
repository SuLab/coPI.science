"""Normalize opportunity_assessments.missing_domains JSONB 'null' to SQL NULL

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-20 00:00:00.000000

Data-only. No schema change: `none_as_null=True` is a Python-side property of the
mapped column, so nothing about the table's DDL moves.

`OpportunityAssessment.missing_domains` documents three states and spells the
middle one NULL — names = a demonstrated gap, NULL = the panel was verified
complete (or none was owed), `[]` = the floor could not be checked at all. But
the column was mapped as a bare `JSONB`, and SQLAlchemy's JSON type defaults
`none_as_null=False`, so Python `None` was written as the JSONB scalar `null`
instead of SQL NULL.

Measured on production immediately before this migration: 15 rows (every row
written since 2026-08-19) held `jsonb_typeof(missing_domains) = 'null'`, and the
18 rows older than that held a true SQL NULL. One logical state, two physical
encodings, in one column. Both deserialize to `None`, so no Python or Jinja
reader was ever wrong; the exposure is a SQL-level reader written against the
documented contract — `WHERE missing_domains IS NULL`, the obvious way to count
verified panels — which silently skipped every recent verified row and counted it
as unverified instead. That inverts the single number the panel instrumentation
exists to produce.

This collapses the two encodings onto the documented one. `[]` is untouched: it
is a JSONB array, not the `null` scalar, and it must stay distinguishable or the
unverified state disappears into the verified one.

Deploy ordering is unconstrained in both directions, unlike 0028/0029/0030. Old
code against normalized data reads `None` exactly as before, and new code against
un-normalized data also reads `None` — the ORM cannot tell the encodings apart,
which is what made the bug invisible for two days.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `jsonb_typeof(...) = 'null'` matches ONLY the JSON null scalar: a SQL NULL
    # input makes jsonb_typeof return SQL NULL (so the predicate is unknown, not
    # true) and an empty array returns 'array'. Both are therefore left alone,
    # which is the whole requirement.
    op.execute(
        """
        UPDATE opportunity_assessments
           SET missing_domains = NULL
         WHERE jsonb_typeof(missing_domains) = 'null'
        """
    )


def downgrade() -> None:
    """Deliberately a no-op.

    After the upgrade a normalized row and a row that was always SQL NULL are
    byte-identical, so there is nothing to tell them apart and no way to restore
    the JSONB 'null' encoding to exactly the rows that carried it. Writing
    something back — turning every SQL NULL into a JSON null — would corrupt the
    18 pre-2026-08-19 rows that never had it, to restore an encoding that was a
    bug in the first place.

    This keeps the downgrade path total: the round trip in scripts/ci.sh runs
    upgrade -> downgrade -> upgrade, and re-running the upgrade after this no-op
    is idempotent because the UPDATE matches nothing the second time.
    """
