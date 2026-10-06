"""PI grant identity, ORCID fundings and job rerun requests

Design: docs/specs/2026-10-05-pi-profile-remediation-design.md §4.2, §6.1, §10 (Phase 1).
Additive:

- ``pi_grant_identity``: one row per PI (``user_id`` is the primary key and a CASCADE FK):
  RePORTER identity status (NULL = not evaluated since the row was made or since staff
  unpinned), accepted and pinned profile ids, the candidate list, the staff
  ``none_confirmed`` flag (NOT NULL DEFAULT false: the table is new), ``evaluated_at`` and
  ``orcid_fetched_at``. ``pinned_by_user_id`` is SET NULL.
- ``pi_orcid_fundings``: one row per ORCID funding group, unique ``(user_id, group_key)``;
  CASCADE with the PI; ``vetoed_by_user_id`` SET NULL.
- ``pi_grants.vetoed_by_user_id`` (uuid, NULL, FK SET NULL): NULL on every row vetoed
  before 0060; never backfilled.
- ``jobs.rerun_requested_at`` / ``jobs.rerun_not_before`` (timestamptz, NULL) and
  ``jobs.rerun_priority`` (smallint, NULL).
- ``users.name_sanitized_at`` (timestamptz, NULL): when an ORCID- or OAuth-sourced name was
  cut to the D60 allowlist; NULL on every existing row, never backfilled.

OLD CODE ON THE NEW SCHEMA is safe: nothing old reads the new tables or columns, and its
inserts leave the new columns NULL. NEW CODE ON THE OLD SCHEMA is not: ``Job`` maps the
rerun columns (every ``select(Job)`` raises UndefinedColumn: the worker's claim, every jobs
page), ``User`` maps ``name_sanitized_at`` (every login and page), ``PiGrant`` maps
``vetoed_by_user_id``, and every persona export selects
``pi_grant_identity`` and ``pi_orcid_fundings`` (UndefinedTable). Migrate BEFORE the new
code serves, with the worker STOPPED (spec §3). The engine imports src.models: rebuild the
agent image with it.

Downgrade drops both tables with every row in them, the FK, the index and the five
columns.

Revision ID: 0060
Revises: 0059
Create Date: 2026-10-05
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Identical to src.models.enrichment.GRANT_IDENTITY_STATUSES (a test pins the equality).
STATUSES = ("resolved", "held", "unconfirmed", "no_match", "firehose", "pinned", "none_confirmed")
_STATUS_SQL = "(" + ", ".join(f"'{s}'" for s in STATUSES) + ")"


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("name_sanitized_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "jobs", sa.Column("rerun_requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "jobs", sa.Column("rerun_not_before", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("jobs", sa.Column("rerun_priority", sa.SmallInteger, nullable=True))

    op.add_column(
        "pi_grants",
        sa.Column("vetoed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_pi_grants_vetoed_by_user_id_users", "pi_grants", "users",
        ["vetoed_by_user_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_pi_grants_vetoed_by_user_id", "pi_grants", ["vetoed_by_user_id"])

    op.create_table(
        "pi_grant_identity",
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("status", sa.String(20), nullable=True),
        sa.Column("accepted_profile_ids", postgresql.ARRAY(sa.Integer), nullable=True),
        sa.Column("candidates", postgresql.JSONB, nullable=True),
        sa.Column("pinned_profile_ids", postgresql.ARRAY(sa.Integer), nullable=True),
        sa.Column("none_confirmed", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column("pinned_by_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("orcid_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"status IS NULL OR status IN {_STATUS_SQL}", name="ck_pi_grant_identity_status"
        ),
    )
    op.create_index(
        "ix_pi_grant_identity_pinned_by_user_id", "pi_grant_identity", ["pinned_by_user_id"]
    )

    op.create_table(
        "pi_orcid_fundings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("group_key", sa.String(200), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("funder_name", sa.String(300), nullable=True),
        sa.Column("funding_type", sa.String(40), nullable=True),
        sa.Column("start_year", sa.Integer, nullable=True),
        sa.Column("start_month", sa.Integer, nullable=True),
        sa.Column("end_year", sa.Integer, nullable=True),
        sa.Column("end_month", sa.Integer, nullable=True),
        sa.Column("external_ids", postgresql.JSONB, nullable=True),
        sa.Column("vetoed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("vetoed_by_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.UniqueConstraint("user_id", "group_key", name="uq_pi_orcid_fundings_user_group"),
    )
    op.create_index(
        "ix_pi_orcid_fundings_vetoed_by_user_id", "pi_orcid_fundings", ["vetoed_by_user_id"]
    )


def downgrade() -> None:
    op.drop_table("pi_orcid_fundings")
    op.drop_table("pi_grant_identity")
    op.drop_index("ix_pi_grants_vetoed_by_user_id", table_name="pi_grants")
    op.drop_constraint("fk_pi_grants_vetoed_by_user_id_users", "pi_grants", type_="foreignkey")
    op.drop_column("pi_grants", "vetoed_by_user_id")
    op.drop_column("jobs", "rerun_priority")
    op.drop_column("jobs", "rerun_not_before")
    op.drop_column("jobs", "rerun_requested_at")
    op.drop_column("users", "name_sanitized_at")
