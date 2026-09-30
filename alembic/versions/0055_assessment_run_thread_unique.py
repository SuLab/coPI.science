"""One assessment row per interview: UNIQUE (simulation_run_id, thread_id)

Spec §8.1/§10.4 (B5). NULL threads do not conflict (Postgres UNIQUE treats
NULLs as distinct, measured on 15.19 — spec C24), so pre-0036 rows and
verdicts whose thread could not be identified are unaffected.

Two prechecks run first, inside the chain's one transaction:

1. refuse while a run is live — the heartbeat row is fresh (updated in the
   last 120 s) with state running, stopping or starting. This is a BACKSTOP:
   the operator's confirmation that nothing runs (Phase 2 rollout step 1) is
   the control, because an old engine's heartbeat can be stale while it is
   still alive;
2. refuse, listing every group, while any duplicate (run, thread) group
   remains. The fix is scripts/migrate/merge_duplicate_assessments.py, which
   runs after 0054 and before this revision.

Downgrade drops the constraint only.

Revision ID: 0055
Revises: 0054
Create Date: 2026-09-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: Union[str, None] = "0054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONSTRAINT = "uq_opportunity_assessments_run_thread"

LIVE_RUN_SQL = (
    "SELECT state, updated_at FROM simulation_process_status "
    "WHERE id = 1 AND state IN ('running', 'stopping', 'starting') "
    "AND updated_at > now() - interval '120 seconds'"
)

DUPLICATE_GROUPS_SQL = (
    "SELECT simulation_run_id::text AS run_id, thread_id, count(*) AS n "
    "FROM opportunity_assessments WHERE thread_id IS NOT NULL "
    "GROUP BY simulation_run_id, thread_id HAVING count(*) > 1 "
    "ORDER BY 1, 2"
)


def precheck(bind) -> None:
    """Raise RuntimeError while a run is live or duplicates remain."""
    live = bind.execute(sa.text(LIVE_RUN_SQL)).first()
    if live is not None:
        raise RuntimeError(
            f"0055 refused: a run looks live (heartbeat state {live.state!r}, "
            f"updated {live.updated_at}). Stop the run and confirm with docker ps "
            "that no engine is up, then retry."
        )
    groups = bind.execute(sa.text(DUPLICATE_GROUPS_SQL)).all()
    if groups:
        listing = "\n".join(f"  run {g.run_id} thread {g.thread_id} x{g.n}" for g in groups)
        raise RuntimeError(
            f"0055 refused: {len(groups)} duplicate (run, thread) group(s) remain:\n"
            f"{listing}\nRun scripts/migrate/merge_duplicate_assessments.py "
            "(dry run, then --apply) and retry."
        )


def upgrade() -> None:
    precheck(op.get_bind())
    op.create_unique_constraint(
        CONSTRAINT, "opportunity_assessments", ["simulation_run_id", "thread_id"]
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, "opportunity_assessments", type_="unique")
