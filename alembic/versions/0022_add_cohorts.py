"""Add cohorts, cohort_memberships and cohort_audit_events

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-30 00:00:00.000000

Renumbered from 0019 at merge time. The cohort branch was cut before main's
db-primary work, so its original "0019" collided with 0019_agent_message_content:
two revisions sharing an id resolve to whichever file sorts last, which silently
skips the other while stamping the DB as fully migrated. Revision ids are assigned
at merge, never at branch. See .notes/cohort-system-v2.md §4.2 / §14 and the
alembic guard in scripts/ci.sh.

Downgrades are idempotent (if_exists) so a rollback cannot wedge on an object that
a partially-applied upgrade never created. See v2 §14.4.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A cohort is a named group of agents permitted to act on each other's
    # activity during simulation. See .notes/cohort-system-v2.md.
    op.create_table(
        "cohorts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=48), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # agent_id is the AgentRegistry slug (no FK — agent rows may not exist at
    # membership-creation time; the app validates at add time).
    op.create_table(
        "cohort_memberships",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "cohort_id",
            UUID(as_uuid=True),
            sa.ForeignKey("cohorts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.String(length=50), nullable=False),
        sa.Column(
            "added_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "cohort_id", "agent_id", name="uq_cohort_membership_cohort_agent"
        ),
    )
    op.create_index(
        "ix_cohort_memberships_cohort_id", "cohort_memberships", ["cohort_id"]
    )
    op.create_index(
        "ix_cohort_memberships_agent_id", "cohort_memberships", ["agent_id"]
    )

    # Append-only audit trail. Deliberately denormalised: a cohort delete cascades
    # its memberships away and a user delete nulls the actor FK, so the trail must
    # not depend on either row surviving — hence cohort_name / actor_email columns
    # and NO FK on cohort_id. `topology` snapshots the full cohort->members map
    # plus the active gate settings at run start and on every change, so a
    # completed simulation run stays attributable to the configuration that
    # produced it (v2 §13.1).
    op.create_table(
        "cohort_audit_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("cohort_id", UUID(as_uuid=True), nullable=True),
        sa.Column("cohort_name", sa.String(length=48), nullable=False),
        sa.Column("agent_id", sa.String(length=50), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column(
            "actor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_email", sa.String(length=255), nullable=True),
        sa.Column("simulation_run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("topology", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_cohort_audit_events_cohort_id", "cohort_audit_events", ["cohort_id"]
    )
    op.create_index(
        "ix_cohort_audit_events_created_at", "cohort_audit_events", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cohort_audit_events_created_at",
        table_name="cohort_audit_events",
        if_exists=True,
    )
    op.drop_index(
        "ix_cohort_audit_events_cohort_id",
        table_name="cohort_audit_events",
        if_exists=True,
    )
    op.drop_table("cohort_audit_events", if_exists=True)
    op.drop_index(
        "ix_cohort_memberships_agent_id",
        table_name="cohort_memberships",
        if_exists=True,
    )
    op.drop_index(
        "ix_cohort_memberships_cohort_id",
        table_name="cohort_memberships",
        if_exists=True,
    )
    op.drop_table("cohort_memberships", if_exists=True)
    op.drop_table("cohorts", if_exists=True)
