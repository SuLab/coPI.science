"""Badge composites on thread_decisions + the 18 unindexed ondelete-FK targets

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-21 00:00:00.000000

Two independent halves of the same finding (issue #25 P1): `AgentBadgeMiddleware`
runs its per-agent proposal COUNT queries — filtered on `(agent_a, outcome)` /
`(agent_b, outcome)` — on every authenticated page load, with no index backing
either predicate; measured ~129x with these indexes in place. Separately, the 18
`(table, column)` pairs below all carry an `ondelete=` foreign key with no index
on the referencing column, so every cascade/SET NULL on the referenced row (and
every join back through the FK) does a sequential scan.

Additive DDL only: safe in either deploy order (old code with the new schema and
new code with the old schema both keep working; the new code merely runs its
existing queries faster once the indexes exist).
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FK_INDEXES = [
    ("access_allowlist", "added_by_user_id"),
    ("agent_delegates", "invitation_id"),
    ("agent_delegates", "user_id"),
    ("agents", "approved_by"),
    ("delegate_invitations", "accepted_by_user_id"),
    ("delegate_invitations", "invited_by_user_id"),
    ("email_notifications", "agent_registry_id"),
    ("email_notifications", "thread_decision_id"),
    ("private_channel_members", "added_by_user_id"),
    ("private_channel_members", "user_id"),
    ("profile_revisions", "changed_by_user_id"),
    ("proposal_reviews", "delegate_user_id"),
    ("proposal_reviews", "reviewed_by_user_id"),
    ("proposal_reviews", "user_id"),
    ("slack_app_provisions", "agent_registry_id"),
    ("cohorts", "created_by"),
    ("cohort_memberships", "added_by"),
    ("cohort_audit_events", "actor_id"),
]


def upgrade() -> None:
    op.create_index(
        "ix_thread_decisions_agent_a_outcome",
        "thread_decisions", ["agent_a", "outcome"],
    )
    op.create_index(
        "ix_thread_decisions_agent_b_outcome",
        "thread_decisions", ["agent_b", "outcome"],
    )
    for table, col in _FK_INDEXES:
        op.create_index(f"ix_{table}_{col}", table, [col])


def downgrade() -> None:
    for table, col in reversed(_FK_INDEXES):
        op.drop_index(f"ix_{table}_{col}", table_name=table)
    op.drop_index(
        "ix_thread_decisions_agent_b_outcome", table_name="thread_decisions"
    )
    op.drop_index(
        "ix_thread_decisions_agent_a_outcome", table_name="thread_decisions"
    )
