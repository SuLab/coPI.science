"""pi_grants, pi_industry_evidence, pi_industry_scores + two job types

Three new tables and two ``job_type_enum`` values (``enrich_grants``,
``industry_evidence``). Purely additive: OLD CODE AGAINST THE NEW SCHEMA IS
SAFE. New code against the old schema fails only in the new worker handlers
and the manager PI page's new panels (UndefinedTable) — see the CLAUDE.md box.

Enum values cannot be dropped in Postgres; downgrade drops the tables and
leaves the values, exactly as 0039 does for ``review_feedback_analysis``.

Revision ID: 0047
Revises: 0046
Create Date: 2026-09-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0047"
down_revision: Union[str, None] = "0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'enrich_grants'")
    op.execute("ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'industry_evidence'")

    op.create_table(
        "pi_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="nih_reporter"),
        sa.Column("core_project_num", sa.String(40), nullable=False),
        sa.Column("reporter_profile_id", sa.Integer, nullable=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("phr_text", sa.Text, nullable=True),
        sa.Column("terms", sa.Text, nullable=True),
        sa.Column("activity_code", sa.String(10), nullable=True),
        sa.Column("agency_ic", sa.String(20), nullable=True),
        sa.Column("funding_mechanism", sa.String(40), nullable=True),
        sa.Column("org_name", sa.String(200), nullable=False),
        sa.Column("first_fy", sa.Integer, nullable=True),
        sa.Column("last_fy", sa.Integer, nullable=True),
        sa.Column("project_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("project_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_award_in_tenure", sa.Integer, nullable=True),
        sa.Column("is_contact_pi", sa.Boolean, nullable=True),
        sa.Column("is_subproject", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("tenure_filter_mode", sa.String(20), nullable=False),
        sa.Column("identity_evidence", postgresql.JSONB, nullable=True),
        sa.Column("vetoed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "core_project_num", name="uq_pi_grants_user_core"),
    )
    op.create_index("ix_pi_grants_user_id", "pi_grants", ["user_id"])

    op.create_table(
        "pi_industry_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("external_id", sa.String(120), nullable=False),
        sa.Column("company_name", sa.String(300), nullable=True),
        sa.Column("company_external_id", sa.String(120), nullable=True),
        sa.Column("company_class", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("year", sa.Integer, nullable=True),
        sa.Column("pi_role", sa.String(30), nullable=True),
        sa.Column("in_tenure", sa.Boolean, nullable=False),
        sa.Column("evidence", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("vetoed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("vetoed_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "source", "kind", "external_id", name="uq_pi_industry_evidence_key"),
    )
    op.create_index("ix_pi_industry_evidence_user_id", "pi_industry_evidence", ["user_id"])

    op.create_table(
        "pi_industry_scores",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("score", sa.Float, nullable=True),
        sa.Column("raw_sum", sa.Float, nullable=True),
        sa.Column("reason", sa.String(40), nullable=True),
        sa.Column("components", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("field_percentile", sa.Float, nullable=True),
        sa.Column("primary_field", sa.String(120), nullable=True),
        sa.Column("tenure_start_used", sa.Integer, nullable=True),
        sa.Column("evidence_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("scorer_version", sa.String(20), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_pi_industry_scores_user_id", "pi_industry_scores", ["user_id"])


def downgrade() -> None:
    op.drop_table("pi_industry_scores")
    op.drop_table("pi_industry_evidence")
    op.drop_table("pi_grants")
    # job_type_enum keeps the two values (cannot drop); harmless at 0046.
