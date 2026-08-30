"""Control-plane service: enqueue/claim one-shot commands, upsert the single
heartbeat/status row, and append to the admin audit trail.

Commands are never desired-state (see src/models/simulation_control.py) — a
`pending` row is a request that exactly one consumer claims once, via
`claim_pending`'s `FOR UPDATE SKIP LOCKED`, and marks done/failed/stale via
`finish_command` / `mark_pending_stale`. `claim_pending` itself never commits:
the row lock has to survive until the caller decides the outcome, which is
exactly what the concurrent-claim test exercises (a second claim on the same
command gets None while the first session's transaction is still open). The
0042 partial unique index (`uq_simulation_commands_one_pending`) enforces at
most one pending row per command kind at the database — `enqueue_command`
lets that `IntegrityError` propagate at flush/commit; callers (the Task 7
routes) catch it and render a refusal, which IS the double-click guard.
"""
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AdminAuditEvent, SimulationCommand, SimulationProcessStatus

#: A status row whose `updated_at` is older than this many seconds is treated
#: as a dead/unresponsive engine rather than trusted as its literal `state`.
HEARTBEAT_STALE_SECONDS = 120


async def enqueue_command(
    db: AsyncSession,
    *,
    command: str,
    payload: dict | None,
    requested_by_user_id,
) -> SimulationCommand:
    """Insert a pending command row.

    May raise `IntegrityError` at flush/commit — the 0042 partial unique
    index allows at most one pending row per command kind. Deliberately not
    caught here: the caller decides what a refused enqueue means.
    """
    cmd = SimulationCommand(
        command=command,
        payload=payload,
        requested_by_user_id=requested_by_user_id,
    )
    db.add(cmd)
    await db.commit()
    return cmd


async def claim_pending(db: AsyncSession, *, command: str) -> SimulationCommand | None:
    """Atomically claim the oldest pending row of `command`.

    Does NOT mark the row's outcome and does NOT commit — the caller holds
    the row lock (inside this same session's open transaction) until it
    calls `finish_command`, which is what makes a concurrent `claim_pending`
    on the same command see nothing to claim (`SKIP LOCKED`) rather than a
    race on the same row.
    """
    result = await db.execute(
        select(SimulationCommand)
        .where(SimulationCommand.status == "pending", SimulationCommand.command == command)
        .order_by(SimulationCommand.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return result.scalar_one_or_none()


async def finish_command(db: AsyncSession, cmd_id, *, status: str, result: str | None) -> None:
    """Mark a claimed command's outcome and commit, releasing its row lock."""
    cmd = await db.get(SimulationCommand, cmd_id)
    if cmd is None:
        return
    cmd.status = status
    cmd.result = result
    cmd.consumed_at = datetime.now(UTC)
    await db.commit()


async def mark_pending_stale(db: AsyncSession, *, reason: str, command: str | None = None) -> int:
    """Flip every pending row (optionally scoped to one command kind) to
    `stale`, recording `reason` as its result. Returns the count flipped.

    `command=None` at supervisor boot stales every pending row regardless of
    kind; `command="start"` after each run stales only stray `start` rows
    (audit V7) without touching a `stop` that might legitimately still be
    pending.
    """
    stmt = (
        update(SimulationCommand)
        .where(SimulationCommand.status == "pending")
        .values(status="stale", result=reason, consumed_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    if command is not None:
        stmt = stmt.where(SimulationCommand.command == command)
    res = await db.execute(stmt)
    await db.commit()
    return res.rowcount


async def upsert_status(
    db: AsyncSession,
    *,
    state: str,
    simulation_run_id=None,
    detail: dict | None = None,
) -> None:
    """INSERT ... ON CONFLICT (id=1) DO UPDATE the single heartbeat row.

    `onupdate` never fires on a `do_update` statement, so `updated_at` is set
    explicitly in both the insert values and the update values rather than
    relied on to bump itself.
    """
    now = datetime.now(UTC)
    stmt = pg_insert(SimulationProcessStatus).values(
        id=1,
        state=state,
        simulation_run_id=simulation_run_id,
        detail=detail,
        updated_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[SimulationProcessStatus.id],
        set_={
            "state": stmt.excluded.state,
            "simulation_run_id": stmt.excluded.simulation_run_id,
            "detail": stmt.excluded.detail,
            "updated_at": now,
        },
    )
    await db.execute(stmt)
    await db.commit()


async def read_status(db: AsyncSession) -> SimulationProcessStatus | None:
    return await db.get(SimulationProcessStatus, 1)


async def record_audit(db: AsyncSession, *, action: str, actor_user_id, payload: dict | None) -> None:
    db.add(AdminAuditEvent(action=action, actor_user_id=actor_user_id, payload=payload))
    await db.commit()


def derive_panel_state(status_row: SimulationProcessStatus | None, now: datetime) -> str:
    """Pure function, no DB: 'not_deployed' when there is no row at all,
    'stale' when the row's `updated_at` is older than
    `HEARTBEAT_STALE_SECONDS`, else the row's own `state` verbatim."""
    if status_row is None:
        return "not_deployed"
    age_seconds = (now - status_row.updated_at).total_seconds()
    if age_seconds > HEARTBEAT_STALE_SECONDS:
        return "stale"
    return status_row.state
