"""Deduplicate publications by (user_id, pmid) and add a unique constraint

Revision ID: 0025
Revises: 0024

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
sync. The pipeline-side dedup landed separately.

`pmid` stays nullable — a DOI-only publication has none, and Postgres unique
constraints treat NULL as distinct from every other NULL, so multiple no-PMID rows
for the same user remain legal.

The dedup step below runs BEFORE the constraint is added: an existing deployment
can already have duplicate rows, and create_unique_constraint fails outright
against a table that violates it.

The keeper is not chosen by ``p.id > p2.id`` (delete the higher id):
``Publication.id`` is ``default=uuid.uuid4`` (src/models/publication.py),
which is uncorrelated with insertion order or richness, so that rule could delete
a rich row (abstract, methods_text, doi, pmcid, journal, year, author_position)
and keep a title-only one. The keeper is now chosen deterministically by
``created_at ASC, id ASC`` (oldest row wins ties by id), and every nullable data
column on the doomed rows is COALESCE-merged into the keeper before the doomed
rows are deleted, so no data is lost regardless of which row the dedup happens
to keep by identity.

Downgrade is idempotent (if_exists) per the 0022+ convention; it drops the
constraint only — the row deletions/merges from upgrade() are not (and cannot
be) undone. Because downgrade() cannot restore them, upgrade() prints the
deleted-row and merged-column counts so the migration log is the only trace of
what a given run changed.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Every nullable data column on Publication (src/models/publication.py) other than
#: the keys (id, user_id, pmid) and created_at itself. `title` is NOT NULL, so it is
#: never merged — the keeper's own title (already required) is left alone.
_MERGE_COLUMNS = (
    "pmcid",
    "doi",
    "abstract",
    "journal",
    "year",
    "author_position",
    "methods_text",
)


def upgrade() -> None:
    # Keep the row with the earliest created_at (tie-broken by id) per (user_id,
    # pmid); pmid IS NOT NULL so distinct-NULL rows (no-PMID publications) are
    # never touched. Merge the doomed rows' data into the keeper before deleting
    # them, since a doomed row can be richer than the one that happens to be kept
    # by identity. RECORD both counts: downgrade() cannot put any of this back, so
    # the migration log is the only trace of what a given run changed.
    conn = op.get_bind()
    dup_rows = conn.execute(
        sa.text(
            f"""
            SELECT id, user_id, pmid, {", ".join(_MERGE_COLUMNS)}
              FROM publications
             WHERE pmid IS NOT NULL
               AND (user_id, pmid) IN (
                     SELECT user_id, pmid FROM publications
                      WHERE pmid IS NOT NULL
                      GROUP BY user_id, pmid
                     HAVING count(*) > 1
                   )
             ORDER BY user_id, pmid, created_at ASC, id ASC
            """
        )
    ).mappings().all()

    groups: dict[tuple, list] = {}
    for row in dup_rows:
        groups.setdefault((row["user_id"], row["pmid"]), []).append(row)

    deleted = 0
    merged_columns = 0
    for rows in groups.values():
        keeper, *doomed = rows  # first row per group is the earliest (ORDER BY above)
        updates = {}
        for col in _MERGE_COLUMNS:
            if keeper[col] is not None:
                continue
            for loser in doomed:
                if loser[col] is not None:
                    updates[col] = loser[col]
                    merged_columns += 1
                    break
        if updates:
            set_clause = ", ".join(f"{c} = :{c}" for c in updates)
            conn.execute(
                sa.text(f"UPDATE publications SET {set_clause} WHERE id = :id"),
                {**updates, "id": keeper["id"]},
            )
        doomed_ids = [loser["id"] for loser in doomed]
        if doomed_ids:
            result = conn.execute(
                sa.text("DELETE FROM publications WHERE id IN :ids").bindparams(
                    sa.bindparam("ids", expanding=True)
                ),
                {"ids": doomed_ids},
            )
            deleted += result.rowcount

    print(
        f"0025: deleted {deleted} duplicate (user_id, pmid) publication rows, "
        f"merged {merged_columns} column value(s) into their keepers"
    )
    op.create_unique_constraint(
        "uq_publications_user_pmid", "publications", ["user_id", "pmid"]
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_publications_user_pmid", "publications", type_="unique", if_exists=True
    )
