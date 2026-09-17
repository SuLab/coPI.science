"""Add agent_messages.sender_user_id and pi_dm_messages.handled_at

Revision ID: 0030
Revises: 0029

Two columns, fixing two ownership gaps:

``agent_messages.sender_user_id`` records who actually wrote a row (NULL for bot
rows and pre-migration rows, which cannot be recovered), so the engine can gate
PI-only side effects (clearing a pending-proposal block, setting ``pi_context``,
the @bot tag route) on the actual writer's owned agents instead of on thread
membership — previously any PI sharing a thread with another PI's agent could
trigger that agent's side effects. ``ON DELETE SET NULL``: deleting a PI's
account must not delete the historical record of what they said.

``pi_dm_messages.handled_at`` is a durable, timestamp-based "has this inbound DM
been processed" marker, replacing three in-memory/cursor mechanisms that all
reset on restart and could each drop a PI message sent while ``agent-run`` was
down. Backfilled to ``created_at`` for existing inbound rows (already handled,
unrecoverable otherwise) so this migration does not replay the DM channel's
entire history; outbound rows are left NULL (unused).

Sizing: the two ADD COLUMNs are catalogue-only. The FK and index on
``agent_messages.sender_user_id`` each scan/lock the full table once; both are
sub-second at current table sizes. If ``agent_messages`` grows very large,
split these into a NOT VALID FK + separate VALIDATE CONSTRAINT and a
CONCURRENTLY index build instead.

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
