"""Add reopened_at to thread_decisions (durable reopen/dedup state)

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-02 00:00:00.000000

_sync_proposal_reviews_from_db reopens a thread whenever a PI submits a
rating=0 (reopen-with-guidance) review, and _reopen_thread reopens one when a
PI tags a closed thread directly (Slack or DB-native). Neither wrote anything
durable: on every restart, _rebuild_agent_state re-closed every thread with a
ThreadDecision row (having no way to tell a reopened one from a still-closed
one), and the only thing that put a reopened thread's ThreadState back was
_sync_proposal_reviews_from_db's own re-processing of the same rating=0 review
row on the next tick — which also re-minted a brand-new synthetic PI-guidance
AgentMessage row and a fresh reply-count budget, forever, once per restart.

reopened_at lets the rebuild recognise a reopened-but-not-yet-reclosed thread
(via its LATEST ThreadDecision row), keep it out of _closed_thread_ids, and
restore its reply budget (message_count_offset) and PI guidance (pi_context)
in the same active-threads reconstruction every other still-open thread
already gets. It also lets the web-guidance reopen flow in
_sync_proposal_reviews_from_db recognise, via the message log content the
rebuild already reloaded, that a prior process already minted this thread's
synthetic PI-guidance row — so every process restores the ThreadState, but
no process re-mints the row. See COR-13.

Nullable, no backfill: NULL means "never reopened", which is the correct
value for every existing row.

Downgrade is idempotent (if_exists) per the 0022+ convention.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "thread_decisions",
        sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("thread_decisions", "reopened_at", if_exists=True)
