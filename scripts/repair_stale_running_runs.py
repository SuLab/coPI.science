"""Mark simulation runs that kept ``status = 'running'`` after their engines died as stopped.

Owner decision D6 (2026-10-19): a run whose stored status is ``running`` but whose engine
is gone is a stale row, and it is repaired to ``stopped`` rather than left to pass as a
live run. Three rows were expected in production: runs 2fd6e53a (2026-07-24), 2485863a
(2026-08-07) and e2b5a03e (2026-08-14).

PREVIEW BY DEFAULT: nothing is written without ``--apply``.

    # list the stale runs and the end time each would get, write nothing
    python scripts/repair_stale_running_runs.py

    # set status = 'stopped' and ended_at on each (one transaction)
    python scripts/repair_stale_running_runs.py --apply

**What counts as stale.** A ``simulation_runs`` row with ``status = 'running'`` that is
not the live run (``live_run_id``) and whose last activity, the greatest of its newest
``llm_call_logs.created_at``, its newest ``agent_messages.created_at`` and its
``started_at``, is older than 24 hours.

**What it writes.** ``status = 'stopped'`` and ``ended_at``: the later of the run's newest
``llm_call_logs.created_at`` and newest ``agent_messages.created_at``, else its
``started_at``, so ``ended_at`` is never earlier than anything the run produced. Nothing else changes (``finalized_at``
and ``held_at`` stay NULL, so the run can still be resumed or finalized from
``/admin/activity``), and nothing is deleted.

**Engine lock.** ``--apply`` holds the engine's advisory lock (``acquire_apply_lock`` from
``scripts/backfill_assessment_headlines.py``) and is refused, exit 2, while an engine or
another lock holder has it. The preview takes no lock.

Exit codes: 0 success; 2 refused (the engine lock is held). Re-running is safe: a repaired
run is no longer ``running``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Prefer the mounted project root over any baked-in copy of `src` in
# site-packages (the image installs src/ non-editable).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.backfill_assessment_headlines import acquire_apply_lock
from src.config import get_settings
from src.models import AgentMessage, LlmCallLog, SimulationRun
from src.services.runs import live_run_id

logger = logging.getLogger("repair_stale_running_runs")

STALE_AFTER = timedelta(hours=24)


@dataclass(frozen=True)
class StaleRun:
    id: uuid.UUID
    started_at: datetime
    #: The proposed ``ended_at``: the run's newest LLM call or message, else its start.
    ended_at: datetime


async def find_stale_runs(db, *, now: datetime) -> list[StaleRun]:
    """The ``running`` runs, other than the live one, idle for more than 24 hours,
    oldest first."""
    live = await live_run_id(db, now=now)
    last_llm = (
        select(func.max(LlmCallLog.created_at))
        .where(LlmCallLog.simulation_run_id == SimulationRun.id)
        .scalar_subquery()
    )
    last_message = (
        select(func.max(AgentMessage.created_at))
        .where(AgentMessage.simulation_run_id == SimulationRun.id)
        .scalar_subquery()
    )
    query = (
        select(SimulationRun.id, SimulationRun.started_at, last_llm, last_message)
        .where(SimulationRun.status == "running")
        .order_by(SimulationRun.started_at, SimulationRun.id)
    )
    if live is not None:
        query = query.where(SimulationRun.id != live)
    cutoff = now - STALE_AFTER
    stale: list[StaleRun] = []
    for run_id, started_at, llm_at, message_at in (await db.execute(query)).all():
        last_activity = max(t for t in (started_at, llm_at, message_at) if t is not None)
        if last_activity < cutoff:
            produced = [t for t in (llm_at, message_at) if t is not None]
            ended_at = max(produced) if produced else started_at
            stale.append(StaleRun(id=run_id, started_at=started_at, ended_at=ended_at))
    return stale


async def repair(db, stale: list[StaleRun]) -> int:
    """Set each run ``stopped`` with its proposed ``ended_at``, in one transaction. A run
    that is no longer ``running`` is left alone. Returns the number of rows updated."""
    updated = 0
    for run in stale:
        result = await db.execute(
            update(SimulationRun)
            .where(SimulationRun.id == run.id, SimulationRun.status == "running")
            .values(status="stopped", ended_at=run.ended_at)
        )
        updated += result.rowcount
    await db.commit()
    return updated


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=(
            "Mark simulation runs stuck at status 'running' with no live engine as "
            "stopped. Preview by default."
        ),
    )
    ap.add_argument(
        "--apply", action="store_true",
        help="Write the previewed repairs (one transaction; needs the engine lock).",
    )
    return ap


async def run_repair(args, factory, *, lock_url: str | None = None) -> int:
    """The whole script, given parsed ``args`` and a session factory. ``lock_url`` is the
    database URL the engine lock connects to (default: the settings URL). Exit codes as
    in the module docstring."""
    lock = None
    if args.apply:
        lock = await acquire_apply_lock(lock_url or get_settings().database_url)
        if lock is None:
            logger.error(
                "refusing to write: a simulation engine holds the engine lock "
                "(stop the engine first)"
            )
            return 2
    try:
        async with factory() as db:
            stale = await find_stale_runs(db, now=datetime.now(timezone.utc))
            for run in stale:
                logger.info(
                    "%s run %s  started %s  ended_at %s",
                    "STOP" if args.apply else "WOULD STOP", run.id,
                    run.started_at.isoformat(), run.ended_at.isoformat(),
                )
            if args.apply:
                count = await repair(db, stale)
                logger.info("stopped %d stale run(s)", count)
            else:
                logger.info("%d stale run(s) to stop (preview)", len(stale))
        return 0
    finally:
        if lock is not None:
            await lock.release()


async def main() -> int:
    args = _build_arg_parser().parse_args()
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        return await run_repair(args, async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(asyncio.run(main()))
