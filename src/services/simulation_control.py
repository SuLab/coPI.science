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
lets that `IntegrityError` propagate at flush/commit; callers (the admin
router's `admin_simulation_start` / `admin_simulation_stop`) catch it and
render a refusal, which IS the double-click guard.

`derive_panel_state` maps the heartbeat row to a panel state; given an
`engine_alive` answer (the engine lock in `pg_locks`, `engine_alive` below) it
applies the liveness table in its docstring, and `panel_state` fetches both.
"""
from datetime import UTC, datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AdminAuditEvent, SimulationCommand, SimulationProcessStatus
from src.services.advisory_locks import ENGINE_LOCK_KEY, advisory_lock_held

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


async def mark_pending_stale(
    db: AsyncSession, *, reason: str, command: str | None = None,
    spare_finalize: bool = False,
) -> int:
    """Flip every pending row (optionally scoped to one command kind) to
    `stale`, recording `reason` as its result. Returns the count flipped.

    `command=None` at supervisor boot stales every pending row regardless of
    kind — except, with `spare_finalize=True`, a `stop` carrying `finalize` and
    a `run_id`: the supervisor runs Finalize run for those once it holds the
    engine lock (spec §8.2, SA5-02). `command="start"` after each run stales
    only stray `start` rows (audit V7) without touching a `stop` that might
    legitimately still be pending.
    """
    stmt = (
        update(SimulationCommand)
        .where(SimulationCommand.status == "pending")
        .values(status="stale", result=reason, consumed_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    if command is not None:
        stmt = stmt.where(SimulationCommand.command == command)
    if spare_finalize:
        # A NULL payload makes the finalize test NULL, and NOT NULL is NULL,
        # which would silently spare every payload-less command too; the
        # explicit IS NULL branch and the COALESCE keep every term non-NULL.
        stmt = stmt.where(
            or_(
                SimulationCommand.payload.is_(None),
                ~(
                    (SimulationCommand.command == "stop")
                    & (func.coalesce(SimulationCommand.payload["finalize"].astext, "false") == "true")
                    & SimulationCommand.payload.has_key("run_id")
                ),
            )
        )
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


def derive_panel_state(
    status_row: SimulationProcessStatus | None,
    now: datetime,
    *,
    engine_alive: bool | None = None,
) -> str:
    """Pure function, no DB.

    With ``engine_alive=None`` (the headline repair script's liveness refusal,
    P0-08) the answer is today's: ``not_deployed`` without a row, ``stale``
    for a row older than ``HEARTBEAT_STALE_SECONDS``, else the row's state.

    With a liveness answer (spec §8.3):

    ========== ============================== ==============
    alive      status row                     panel state
    ========== ============================== ==============
    no         none                           not_deployed
    no         ``starting`` and fresh         starting
    no         anything else                  idle
    yes        ``running``/``stopping`` fresh that state
    yes        ``starting``/``idle`` fresh    starting
    yes        anything else (stale, none)    unresponsive
    ========== ============================== ==============
    """
    fresh = (
        status_row is not None
        and (now - status_row.updated_at).total_seconds() <= HEARTBEAT_STALE_SECONDS
    )
    if engine_alive is None:
        if status_row is None:
            return "not_deployed"
        return status_row.state if fresh else "stale"
    if not engine_alive:
        if status_row is None:
            return "not_deployed"
        if fresh and status_row.state == "starting":
            return "starting"
        return "idle"
    if fresh and status_row.state in ("running", "stopping"):
        return status_row.state
    if fresh and status_row.state in ("starting", "idle"):
        return "starting"
    return "unresponsive"


async def engine_alive(db: AsyncSession) -> bool:
    """True while some process in this database holds the engine lock (B4).

    The authoritative liveness signal from Phase 2 on: the heartbeat row can be
    stale while an engine is alive (a long tick, S1-05) and fresh for up to
    ``HEARTBEAT_STALE_SECONDS`` after one died, but the lock lives exactly as
    long as the engine's dedicated connection."""
    return await advisory_lock_held(db, ENGINE_LOCK_KEY)


async def panel_state(db: AsyncSession, now: datetime) -> tuple[str, SimulationProcessStatus | None, bool]:
    """The page's panel state with liveness: ``(state, status_row, alive)``."""
    row = await read_status(db)
    alive = await engine_alive(db)
    return derive_panel_state(row, now, engine_alive=alive), row, alive


def is_finalize_stop(cmd) -> bool:
    """A ``stop`` command enqueued by Finalize run (spec §8.2)."""
    payload = getattr(cmd, "payload", None) or {}
    return (
        getattr(cmd, "command", None) == "stop"
        and bool(payload.get("finalize"))
        and bool(payload.get("run_id"))
    )
