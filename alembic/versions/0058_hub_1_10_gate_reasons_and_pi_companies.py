"""Hub 1.10.0: gating_rationales, pi_companies, and the company_discovery job type

Design: the 2026-10-02 hub 1.10.0 spec (docs/specs/2026-10-02-hub-1-10-summary-risks-gates-design.md),
§5.1 and §7.1. Additive:

- ``opportunity_assessments.gating_rationales`` (JSONB, NULL): the hub's one-sentence
  reason per gate under scout_hub 1.10.0, keyed like ``gating``. NULL means the row
  predates 0058 or the value was malformed; never backfilled.
- ``pi_companies``: the staff Companies list per PI (src/models/pi_company.py). Rows
  CASCADE with the PI; the two staff attribution columns are SET NULL.
- ``company_discovery`` joins ``job_type_enum``, and ``uq_jobs_one_active_per_user_type``
  is rebuilt under the same name with it in the predicate, so a PI has at most one
  pending or processing discovery job. The predicate still tests ``job_type_text(type)``
  (0056): Postgres refuses to use an enum value in the transaction that added it, and a
  fresh chain runs 0039 through 0058 in one transaction. No precheck: no
  ``company_discovery`` row can exist before this revision.

OLD CODE ON THE NEW SCHEMA is safe until the first ``company_discovery`` job row exists:
nothing old reads the column or the table, but the old ``Job`` model does not know the
new enum value, so an old process that loads such a row (the worker's claim, the jobs
pages) raises LookupError. Bring the new web app and worker up together.
NEW CODE ON THE OLD SCHEMA is not safe: ``OpportunityAssessment`` maps
``gating_rationales`` (UndefinedColumn on every select, and the engine's best-effort
verdict write loses every verdict), and the manager PI page and every profile export
select ``pi_companies`` (UndefinedTable). Migrate BEFORE the new code serves, with the
worker idle (the index rebuild takes ACCESS EXCLUSIVE on ``jobs``) and no live run.

Downgrade drops the column, the table and every row in it, and restores 0056's
predicate. ``job_type_enum`` keeps ``company_discovery`` (Postgres has no DROP VALUE), as
0039 and 0047 keep theirs; delete the ``company_discovery`` job rows before 0057 code
runs against the database, because that code cannot load them.

Revision ID: 0058
Revises: 0057
Create Date: 2026-10-02
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PER_USER_0056 = "('generate_profile','enrich_grants','industry_evidence')"
_PER_USER = "('generate_profile','enrich_grants','industry_evidence','company_discovery')"

#: 0056's predicate, which the downgrade restores (a test pins it to 0056's own constant).
ONE_ACTIVE_WHERE_0056 = (
    "status IN ('pending','processing') AND user_id IS NOT NULL "
    f"AND job_type_text(type) IN {_PER_USER_0056}"
)
#: The widened predicate; identical to src.models.job.ONE_ACTIVE_PER_USER_TYPE_WHERE (a
#: test pins the equality), because ``ON CONFLICT ... WHERE`` infers the index only when
#: its predicate matches.
ONE_ACTIVE_WHERE = (
    "status IN ('pending','processing') AND user_id IS NOT NULL "
    f"AND job_type_text(type) IN {_PER_USER}"
)


def upgrade() -> None:
    # PG15 allows ADD VALUE inside the chain's one transaction as long as nothing in it
    # uses the value; the rebuilt predicate below compares text, so nothing does. IF NOT
    # EXISTS keeps ci.sh's up->down->up idempotent, because downgrade() cannot drop it.
    op.execute("ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'company_discovery'")

    op.add_column(
        "opportunity_assessments",
        sa.Column("gating_rationales", postgresql.JSONB, nullable=True),
    )

    op.create_table(
        "pi_companies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_name", sa.String(200), nullable=False),
        sa.Column("normalized_name", sa.String(200), nullable=False),
        sa.Column("pi_role", sa.String(20), nullable=False),
        sa.Column("funding_usd", sa.BigInteger, nullable=True),
        sa.Column("funding_as_of", sa.Date, nullable=True),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("funding_source_url", sa.Text, nullable=True),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("origin", sa.String(10), nullable=False),
        sa.Column("evidence", postgresql.JSONB, nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("reviewed_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "normalized_name", name="uq_pi_companies_user_normalized_name"),
        sa.CheckConstraint(
            "pi_role IN ('founder', 'co_founder', 'board', 'advisor')", name="ck_pi_companies_pi_role",
        ),
        sa.CheckConstraint("funding_usd >= 0", name="ck_pi_companies_funding_usd"),
        sa.CheckConstraint(
            "status IN ('suggested', 'confirmed', 'rejected')", name="ck_pi_companies_status",
        ),
        sa.CheckConstraint("origin IN ('manual', 'discovered')", name="ck_pi_companies_origin"),
    )
    op.create_index("ix_pi_companies_user_id", "pi_companies", ["user_id"])

    # Same name, wider predicate. scripts/migrate/preflight.py lists this index under
    # PLANNED_RECREATES, not PLANNED_OBJECTS: its pre-existence is this revision's
    # precondition, not a collision.
    op.drop_index("uq_jobs_one_active_per_user_type", table_name="jobs")
    op.create_index(
        "uq_jobs_one_active_per_user_type", "jobs", ["user_id", "type"], unique=True,
        postgresql_where=sa.text(ONE_ACTIVE_WHERE),
    )


def downgrade() -> None:
    op.drop_index("uq_jobs_one_active_per_user_type", table_name="jobs")
    op.create_index(
        "uq_jobs_one_active_per_user_type", "jobs", ["user_id", "type"], unique=True,
        postgresql_where=sa.text(ONE_ACTIVE_WHERE_0056),
    )
    op.drop_table("pi_companies")
    op.drop_column("opportunity_assessments", "gating_rationales")
    # job_type_enum keeps 'company_discovery': see the module docstring.
