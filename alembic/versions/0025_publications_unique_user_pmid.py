"""Deduplicate publications by (user_id, pmid) and add a unique constraint

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-02 00:00:00.000000

publications has never had a uniqueness guarantee on (user_id, pmid)
(models/publication.py had no __table_args__; 0001_initial.py:121-122 created only
non-unique indexes). profile_pipeline.py builds the `pmids` list from an ORCID works
listing with no dedup (a work linked to two affiliation-groups, or a DOI-only work
whose resolved PMID matches one already in `pmids`, lists the same PMID twice) and
the `existing_pubs` lookup dict used to skip re-inserts was built once before the
insert loop and never updated inside it — so a PMID appearing twice in one ORCID
works listing inserted two Publication rows in a single pipeline run. Downstream
this silently: (a) made `scalar_one_or_none()` in the PMC-methods step raise
MultipleResultsFound, swallowed at a debug log; (b) duplicated citation lines in the
exported markdown; (c) inflated admin.py's publication counts; (d) was a multiplier
on the join `_load_publication_records` (simulation.py) re-runs on every roster
sync. See issue #22 COR-16 (the pipeline-side dedup landed separately, same issue).

`pmid` stays nullable — a DOI-only publication has none, and Postgres unique
constraints treat NULL as distinct from every other NULL, so multiple no-PMID rows
for the same user remain legal.

The dedup step below runs BEFORE the constraint is added: an existing deployment
can already have duplicate rows, and create_unique_constraint fails outright
against a table that violates it.

Downgrade is idempotent (if_exists) per the 0022+ convention; it drops the
constraint only — the row deletions from upgrade() are not (and cannot be) undone.
Because downgrade() cannot restore them, upgrade() prints the deleted-row count so the
migration log is the only trace of what a given run destroyed.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Keep the lowest id per (user_id, pmid); pmid IS NOT NULL so distinct-NULL
    # rows (no-PMID publications) are never touched. RECORD the count: downgrade()
    # cannot put these rows back, so the migration log is the only trace.
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            """
            DELETE FROM publications p
            USING publications p2
            WHERE p.pmid IS NOT NULL
              AND p.user_id = p2.user_id
              AND p.pmid = p2.pmid
              AND p.id > p2.id
            """
        )
    )
    print(f"0025: deleted {result.rowcount} duplicate (user_id, pmid) publication rows")
    op.create_unique_constraint(
        "uq_publications_user_pmid", "publications", ["user_id", "pmid"]
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_publications_user_pmid", "publications", type_="unique", if_exists=True
    )
