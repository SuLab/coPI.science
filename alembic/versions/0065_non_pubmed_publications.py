"""Non-PubMed publications: 'openalex' provenance, one PMID-less row per PI per DOI

Design: docs/specs/2026-10-09-thin-profile-fallback-design.md §5.4 (Phase B).

- ``ck_publications_provenance`` also admits ``'openalex'``: a work PubMed does not have,
  stored for a PubMed-thin PI because OpenAlex attributes it to the PI's ORCID (owner
  decision D6, no staff review).
- ``uq_publications_user_doi_nopmid``: a unique index on ``(user_id, lower(doi))`` WHERE
  ``pmid IS NULL``. Production had 0 PMID-less rows on 2026-10-09, so it builds clean.

OLD CODE ON THE NEW SCHEMA is safe: it never writes 'openalex' or a PMID-less row. NEW CODE
ON THE OLD SCHEMA is not: storing a non-PubMed work violates the CHECK and the generation
fails. Migrate BEFORE the new worker serves, with the worker idle (the CHECK swap and the index
build lock ``publications``; the chain's 10 s lock_timeout otherwise trips). The engine never
queries publications; the agent image is rebuilt in the same deploy for the abstract-tool
fallback, not for this schema.

Downgrade drops the index and restores 0062's CHECK, relabelling every PMID-less row this
corpus stored ('openalex', 's1', 's1,s2': any provenance but NULL, 'manual' and
'unanchored') as 'unanchored' first: the old CHECK refuses 'openalex', and the old scripts
would otherwise meet PMID-less rows labelled as anchored. They then show on the manager's
Unanchored papers card.

Revision ID: 0065
Revises: 0064
Create Date: 2026-10-09
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Identical to src.models.publication.PROVENANCE_CHECK_SQL (a test pins the equality).
PROVENANCE_CHECK_SQL = (
    "provenance IS NULL OR provenance IN ('manual', 'unanchored', 'openalex') "
    "OR provenance ~ '^(s[1-4],)*s[13](,s[1-4])*$'"
)
#: 0062's CHECK, restored by downgrade().
PREVIOUS_PROVENANCE_CHECK_SQL = (
    "provenance IS NULL OR provenance IN ('manual', 'unanchored') "
    "OR provenance ~ '^(s[1-4],)*s[13](,s[1-4])*$'"
)


def upgrade() -> None:
    op.drop_constraint("ck_publications_provenance", "publications", type_="check")
    op.create_check_constraint(
        "ck_publications_provenance", "publications", PROVENANCE_CHECK_SQL
    )
    op.create_index(
        "uq_publications_user_doi_nopmid", "publications", ["user_id", sa.text("lower(doi)")],
        unique=True, postgresql_where=sa.text("pmid IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_publications_user_doi_nopmid", table_name="publications")
    op.execute(
        "UPDATE publications SET provenance = 'unanchored' WHERE pmid IS NULL "
        "AND provenance IS NOT NULL AND provenance NOT IN ('manual', 'unanchored')"
    )
    op.drop_constraint("ck_publications_provenance", "publications", type_="check")
    op.create_check_constraint(
        "ck_publications_provenance", "publications", PREVIOUS_PROVENANCE_CHECK_SQL
    )
