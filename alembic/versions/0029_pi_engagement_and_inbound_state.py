"""Add thread_decisions.pi_engaged_at and agent_messages.pi_inbound_state

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-04 00:00:00.000000

Two nullable markers, one per open item on issue #20. Both are nullable with no
server default and no backfill: NULL means "unknown", and every reader must
treat unknown as the pre-0029 behaviour. Rulings and full read semantics:
docs/plans/2026-09-04-decisions/task-8.md and task-7.md.

thread_decisions.pi_engaged_at (COR-5)
--------------------------------------
_check_pi_proposal_review clears an agent's pending-proposal block when the
owning PI engages with the thread, but _persist_implicit_proposal_review can
only record that durably as a ProposalReview row, and proposal_reviews.user_id
is NOT NULL — so an agent whose AgentRegistry.user_id is NULL (a bulk-provisioned
or self-deleted-account agent) gets an in-memory-only clear, and
_rebuild_agent_state re-blocks the proposal on the next restart.

The obvious fix — make proposal_reviews.user_id nullable — was rejected: that
column is ForeignKey("users.id", ondelete="CASCADE") (src/models/agent_registry
.py:82-84), so deleting a PI would delete the engine's own block-clearing
markers and every proposal that PI ever engaged with would re-block after the
next restart. A timestamp on the thread's own decision row has no CASCADE
exposure to users at all (thread_decisions cascades from simulation_runs only).

NULL means "no PI engagement recorded", which is the correct value for every
existing row: the rebuild's re-block is exactly what those rows have always
produced.

agent_messages.pi_inbound_state (COR-10(3))
-------------------------------------------
The DB inbound poller (_poll_inbound_from_db) is moving its MessageLog append
AHEAD of _handle_pi_inbound_entry, so a handler failure can no longer lose the
PI's text. But the log entry's presence WAS the dedup key, so once the append
runs first the retry is skipped and the side effects are lost instead. Dedup
therefore has to read a marker that is distinct from the log entry.

Three states, because two are not enough. A plain "handled_at" timestamp cannot
tell "no poller has claimed this row" from "the poller claimed it and its
handler failed", and the difference is load-bearing: _poll_channels appends a
Slack-origin PI message to the log itself and applies the same side effects
inline, then persists it as an agent_messages row that _poll_inbound_from_db
re-reads (it filters on simulation_run_id and created_at only). Under a
two-valued marker that row reads as unhandled and gets its side effects applied
a second time, including the @bot tag route — a duplicate reply on every tagged
Slack message.

    NULL        unknown: no DB inbound poller has claimed this row. Every
                pre-0029 row, every bot/agent-authored row, and every row some
                other path put in the log. Readers fall back to today's
                behaviour (dedup on MessageLog presence).
    'ingested'  the poller appended this row's text to the log and has NOT yet
                confirmed _handle_pi_inbound_entry succeeded. Re-run it.
    'handled'   _handle_pi_inbound_entry returned without raising. Skip, and
                advance the cursor.

Written only by SimulationEngine._poll_inbound_from_db, only for is_bot=false
rows. Deliberately a plain String, not an enum type: an enum would need its own
CREATE TYPE (and a DROP TYPE in the downgrade) for a value set only one function
writes, and 0020's pi_dm_direction_enum is the only precedent for spending that.

Sizing: both are ADD COLUMN ... NULL with no default, which Postgres 11+ applies
as a catalogue-only change (no table rewrite, no per-row work) under a brief
ACCESS EXCLUSIVE lock, regardless of agent_messages' size. Measured figures are
in docs/plans/2026-09-04-decisions/task-8.md.

Unlike 0026 there is no constraint whose name could have drifted, so no
pg_constraint resolution is needed here; the equivalent defence is the
if_exists=-guarded downgrade (the branch convention since 0022), so a
half-applied or hand-repaired database cannot abort the downgrade partway.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "thread_decisions",
        sa.Column("pi_engaged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "agent_messages",
        sa.Column("pi_inbound_state", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_messages", "pi_inbound_state", if_exists=True)
    op.drop_column("thread_decisions", "pi_engaged_at", if_exists=True)
