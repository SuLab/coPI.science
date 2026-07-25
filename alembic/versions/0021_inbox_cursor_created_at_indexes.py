"""Index the DB inbox pollers' created_at cursor

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-25 00:00:00.000000

Both DB inbox pollers used to page over ``posted_at``, which is derived from the
*writing process's* clock (float of its minted ts). That made inbound PI delivery
depend on every writer's clock agreeing with the engine's to within the lookback
window — fine on one host, silently lossy across hosts. They now page over
``created_at`` (``server_default=now()``, i.e. the single Postgres server's
clock), so these indexes back the new access path. See
.notes/db-conversations-residual-2026-07-24.md (R3).
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_agent_messages_run_created", "agent_messages",
        ["simulation_run_id", "created_at"],
    )
    op.create_index(
        "ix_pi_dm_run_direction_created", "pi_dm_messages",
        ["simulation_run_id", "direction", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_pi_dm_run_direction_created", table_name="pi_dm_messages")
    op.drop_index("ix_agent_messages_run_created", table_name="agent_messages")
