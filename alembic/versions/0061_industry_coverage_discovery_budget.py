"""Industry score coverage, company discovery COI usage and ledger

Design: docs/specs/2026-10-05-pi-profile-remediation-design.md §6.2, §10 (Phase 2).
Additive:

- ``pi_industry_scores.coverage`` (JSONB, NULL): per evidence source, "ok", "truncated" or
  "unavailable:<reason>" for the run that wrote the row. NULL on every row written before
  0061; never backfilled.
- ``company_discovery_usage``: one row per COI extraction call, reserved before the call and
  settled after it; the rolling-24h dollar ceiling sums it. ``user_id`` CASCADE.
- ``company_discovery_coi_ledger``: one row per (PI, PMID, statement hash, name-forms hash):
  the outcome and verified claims of that statement's last extraction, and how many
  billed failed extractions it has had (``attempts``). ``user_id`` CASCADE.

OLD CODE ON THE NEW SCHEMA is safe: nothing old reads the new column or tables, and its
score-row inserts leave ``coverage`` NULL. NEW CODE ON THE OLD SCHEMA is not:
``PiIndustryScore`` maps ``coverage`` (the manager PI list and detail pages raise
UndefinedColumn) and company discovery writes both tables (UndefinedTable fails the job).
Migrate BEFORE the new code serves, with the worker STOPPED (spec §3). The engine imports
src.models: rebuild the agent image with it.

Downgrade drops both tables with every row in them, and the column.

Revision ID: 0061
Revises: 0060
Create Date: 2026-10-06
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Identical to src.models.pi_company.COI_USAGE_STATUSES and COI_LEDGER_OUTCOMES (a test
#: pins the equality).
USAGE_STATUSES = ("reserved", "settled")
LEDGER_OUTCOMES = ("ok", "skipped", "unavailable")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.add_column("pi_industry_scores", sa.Column("coverage", postgresql.JSONB, nullable=True))

    op.create_table(
        "company_discovery_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("pmid", sa.String(20), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("served_by_model", sa.String(100), nullable=True),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("reserved_usd", sa.Numeric(10, 4), nullable=False),
        sa.Column("cost_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("input_tokens", sa.Integer, nullable=True),
        sa.Column("output_tokens", sa.Integer, nullable=True),
        sa.Column("cache_read_input_tokens", sa.Integer, nullable=True),
        sa.Column("cache_creation_input_tokens", sa.Integer, nullable=True),
        sa.Column("usage_by_model", postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"status IN ({_in(USAGE_STATUSES)})", name="ck_company_discovery_usage_status"
        ),
    )
    op.create_index(
        "ix_company_discovery_usage_created", "company_discovery_usage", ["created_at"]
    )
    op.create_index(
        "ix_company_discovery_usage_user_id", "company_discovery_usage", ["user_id"]
    )

    op.create_table(
        "company_discovery_coi_ledger",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("pmid", sa.String(20), nullable=False),
        sa.Column("statement_hash", sa.String(64), nullable=False),
        sa.Column("name_forms_hash", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(12), nullable=False),
        sa.Column("claims", postgresql.JSONB, nullable=True),
        sa.Column("attempts", sa.Integer, server_default="0", nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "user_id", "pmid", "statement_hash", "name_forms_hash",
            name="uq_company_discovery_coi_ledger_key",
        ),
        sa.CheckConstraint(
            f"outcome IN ({_in(LEDGER_OUTCOMES)})",
            name="ck_company_discovery_coi_ledger_outcome",
        ),
    )


def downgrade() -> None:
    op.drop_table("company_discovery_coi_ledger")
    op.drop_table("company_discovery_usage")
    op.drop_column("pi_industry_scores", "coverage")
