"""Add agent_messages.sender_user_id and pi_dm_messages.handled_at

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-08 00:00:00.000000

Two columns, one per open item on audit findings RC-1 and RC-2
(docs/plans/2026-09-08-audit-fixes.md).

agent_messages.sender_user_id (RC-1 / #20 COR-5)
-------------------------------------------------
``agent_messages`` carries no sender identity, so
``SimulationEngine._handle_pi_inbound_entry`` used the thread's own
participants as a stand-in for "who is allowed to act on this thread" —
which means PI A, acting in a thread A's agent shares with B's agent, could
clear B's pending-proposal block, set B's ``pi_context``, or drive B's @bot
tag route. This column records who actually wrote the row (NULL for every
bot-authored row, and for every row written before this migration — there is
no way to recover that identity, so no backfill is attempted). The engine
resolves it to an owned-agent set via ``AgentRegistry.user_id`` and
``AgentDelegate`` and gates every side effect on that set instead of on
thread membership.

``ON DELETE SET NULL``, not CASCADE: deleting a PI's account must not delete
the historical record of what they said in a thread another PI's agent is
also a party to. A NULL ``sender_user_id`` after this migration means either
a bot row, a pre-migration row, or a since-deleted user — all three get no
ownership-gated side effect from that row, which is the correct (fail-closed)
answer for all three.

pi_dm_messages.handled_at (RC-2)
---------------------------------
Three cooperating mechanisms conspired to lose every side effect of a PI
message written while ``agent-run`` was down: ``record_pi_message`` never
marked a row as needing attention, ``_seed_pi_dm_cursor``/``_seed_pi_inbox_cursor``
jump their cursors to ``max(created_at)`` at startup (skipping anything older),
and the in-memory dedup sets (``_pi_dm_seen``) reset to empty on every restart
so they could not remember what was already handled even within the lookback
window. ``handled_at`` is the durable, timestamp-based counterpart of
``agent_messages.pi_inbound_state`` for the DM channel: NULL means "no
``_poll_pi_dms_from_db`` tick has processed this row yet", set once (success
or failure — a DM gets one attempt) after ``PIHandler.handle_dm`` returns.

Backfilled for existing inbound rows in this same migration
(``handled_at = created_at`` where ``direction = 'inbound' AND handled_at IS
NULL``): every one of those rows has already been through the handler (or is
long enough in the past to be unrecoverable), and leaving them NULL would
have a deploy of this fix re-run ``handle_dm`` against the DM channel's
entire history the moment the new query ships. Outbound rows are left NULL
(the column is meaningless for them; nothing reads it).

Sizing: two ADD COLUMNs (ADD COLUMN ... NULL, no default) are catalogue-only,
no table rewrite, under a brief ACCESS EXCLUSIVE lock, regardless of table
size — same shape as 0028/0029. The backfill UPDATE is bounded by the number
of existing inbound ``pi_dm_messages`` rows (small: one per DM ever sent while
Slack/the web DM path has been live) and runs under the same migration
transaction as the two ADD COLUMNs.

Downgrade is idempotent (if_exists) per the 0022+ convention.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SENDER_USER_ID_FK = "agent_messages_sender_user_id_fkey"


def upgrade() -> None:
    op.add_column(
        "agent_messages",
        sa.Column("sender_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        _SENDER_USER_ID_FK,
        "agent_messages", "users", ["sender_user_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(
        "ix_agent_messages_sender_user_id", "agent_messages", ["sender_user_id"]
    )
    op.add_column(
        "pi_dm_messages",
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE pi_dm_messages SET handled_at = created_at "
        "WHERE direction = 'inbound' AND handled_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("pi_dm_messages", "handled_at", if_exists=True)
    op.drop_index(
        "ix_agent_messages_sender_user_id", table_name="agent_messages", if_exists=True
    )
    op.drop_constraint(
        _SENDER_USER_ID_FK, "agent_messages", type_="foreignkey", if_exists=True
    )
    op.drop_column("agent_messages", "sender_user_id", if_exists=True)
