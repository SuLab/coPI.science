"""Corpus provenance and review, profile drafts, persona export failure flag

Design: docs/specs/2026-10-05-pi-profile-remediation-design.md §4.3, §6.3, §10 (Phase 3).
Additive:

- ``publications.provenance`` (varchar(20), NULL; CHECK ``ck_publications_provenance``):
  "manual", "unanchored" or an anchored record's stage list ("s1,s4"). NULL on existing rows; a
  profile run or scripts/unanchored_publications_report.py --apply writes it.
- ``publications.excluded_at`` / ``excluded_by_user_id`` (FK SET NULL, indexed): staff
  exclusion; the row is kept.
- ``publications.doi_verified`` (boolean, NULL): the DOI matches PubMed's record for the
  PMID. NULL on existing rows (the export then prefers the PubMed link).
- ``publication_candidates``: unanchored finds held for staff review, unique
  ``(user_id, pmid)``; ``user_id`` CASCADE, ``decided_by_user_id`` SET NULL.
- ``researcher_profiles.human_edited_at`` (timestamptz, NULL) and
  ``researcher_profiles.evidence_flagged_count`` (integer, NULL).
- ``agents.persona_export_failed_at`` (timestamptz, NULL).

DATA: ``human_edited_at`` is backfilled (spec D55, an explicit exception to "NULL is not
backfilled") from web / web_impersonated public revisions newer than the profile's
generation that changed its Research Summary or tag sections. The statement is idempotent
and is re-run by scripts/backfill_human_edited_at.py after the new code serves.

OLD CODE ON THE NEW SCHEMA is safe: nothing old reads the new columns or table, its inserts
leave them NULL, and it ignores provenance and exclusions. NEW CODE ON THE OLD SCHEMA is
not: ``Publication``, ``ResearcherProfile`` and ``AgentRegistry`` map the new columns, so
every PI page, profile save, login-time agent lookup and the worker raise UndefinedColumn.
Migrate BEFORE the new code serves, with the worker STOPPED (spec §3). The engine imports
src.models: rebuild the agent image with it.

Downgrade drops the table with every row in it, the constraints, the index and the eight
columns (the backfilled values go with ``human_edited_at``).

Revision ID: 0062
Revises: 0061
Create Date: 2026-10-06
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Identical to src.models.publication.PROVENANCE_CHECK_SQL and CANDIDATE_STATUSES (a test
#: pins the equality).
PROVENANCE_CHECK_SQL = (
    "provenance IS NULL OR provenance IN ('manual', 'unanchored') "
    "OR provenance ~ '^(s[1-4],)*s[13](,s[1-4])*$'"
)
CANDIDATE_STATUSES = ("pending", "accepted", "rejected")

#: Spec 2026-10-05 D55: human_edited_at from web / web_impersonated public revisions newer
#: than the profile's generation whose Research Summary or tag sections differ from the
#: preceding public revision. The compared text is the persona between its header and the
#: first of Recent Publications / Active Grants / Past Grants. Never moves the value
#: backward, so it is safe to re-run.
HUMAN_EDITED_AT_BACKFILL_SQL = r"""
WITH bodies AS (
    SELECT id, agent_registry_id, mechanism, created_at,
           regexp_replace(content, E'\n## (Recent Publications|Active Grants|Past Grants).*$', '')
               AS body
      FROM profile_revisions
     WHERE profile_type = 'public'
), cores AS (
    SELECT id, agent_registry_id, mechanism, created_at,
           CASE WHEN strpos(body, E'\n## ') > 0 THEN substr(body, strpos(body, E'\n## '))
                ELSE '' END AS core
      FROM bodies
), seq AS (
    SELECT cores.*,
           lag(core) OVER (PARTITION BY agent_registry_id ORDER BY created_at, id) AS prev_core
      FROM cores
), edits AS (
    SELECT a.user_id, max(s.created_at) AS edited_at
      FROM seq s
      JOIN agents a ON a.id = s.agent_registry_id
      JOIN researcher_profiles p ON p.user_id = a.user_id
     WHERE s.mechanism IN ('web', 'web_impersonated')
       AND s.created_at > coalesce(p.profile_generated_at, '-infinity')
       AND s.prev_core IS DISTINCT FROM s.core
     GROUP BY a.user_id
)
UPDATE researcher_profiles p
   SET human_edited_at = e.edited_at
  FROM edits e
 WHERE p.user_id = e.user_id
   AND (p.human_edited_at IS NULL OR p.human_edited_at < e.edited_at)
"""


def upgrade() -> None:
    op.add_column("publications", sa.Column("provenance", sa.String(20), nullable=True))
    op.add_column(
        "publications", sa.Column("excluded_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "publications",
        sa.Column("excluded_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("publications", sa.Column("doi_verified", sa.Boolean, nullable=True))
    op.create_foreign_key(
        "fk_publications_excluded_by_user_id_users", "publications", "users",
        ["excluded_by_user_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(
        "ix_publications_excluded_by_user_id", "publications", ["excluded_by_user_id"]
    )
    op.create_check_constraint("ck_publications_provenance", "publications", PROVENANCE_CHECK_SQL)

    op.create_table(
        "publication_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pmid", sa.String(20), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("year", sa.Integer, nullable=True),
        sa.Column("stages", sa.String(20), nullable=True),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("decided_by_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.UniqueConstraint("user_id", "pmid", name="uq_publication_candidates_user_pmid"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in CANDIDATE_STATUSES) + ")",
            name="ck_publication_candidates_status",
        ),
    )
    op.create_index(
        "ix_publication_candidates_decided_by_user_id", "publication_candidates",
        ["decided_by_user_id"],
    )

    op.add_column(
        "researcher_profiles",
        sa.Column("human_edited_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "researcher_profiles", sa.Column("evidence_flagged_count", sa.Integer, nullable=True)
    )
    op.add_column(
        "agents", sa.Column("persona_export_failed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(HUMAN_EDITED_AT_BACKFILL_SQL)


def downgrade() -> None:
    op.drop_column("agents", "persona_export_failed_at")
    op.drop_column("researcher_profiles", "evidence_flagged_count")
    op.drop_column("researcher_profiles", "human_edited_at")
    op.drop_table("publication_candidates")
    op.drop_constraint("ck_publications_provenance", "publications", type_="check")
    op.drop_index("ix_publications_excluded_by_user_id", table_name="publications")
    op.drop_constraint(
        "fk_publications_excluded_by_user_id_users", "publications", type_="foreignkey"
    )
    op.drop_column("publications", "doi_verified")
    op.drop_column("publications", "excluded_by_user_id")
    op.drop_column("publications", "excluded_at")
    op.drop_column("publications", "provenance")
