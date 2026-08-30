"""Control-plane rows: commands are one-shot and claimable exactly once;
the status row is a single upserted heartbeat; audit events are append-only."""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import AdminAuditEvent, SimulationCommand, SimulationProcessStatus
from src.services.simulation_control import (
    HEARTBEAT_STALE_SECONDS,
    claim_pending,
    derive_panel_state,
    enqueue_command,
    finish_command,
    mark_pending_stale,
    read_status,
    upsert_status,
)


@pytest.mark.asyncio
async def test_command_and_status_rows_round_trip(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        cmd = SimulationCommand(command="start", payload={"fresh": True, "max_runtime": 60})
        status = SimulationProcessStatus(id=1, state="idle")
        audit = AdminAuditEvent(action="simulation_start_requested", payload={"fresh": True})
        db.add_all([cmd, status, audit])
        await db.commit()
        cmd_id = cmd.id
    async with factory() as db:
        row = (await db.execute(
            select(SimulationCommand).where(SimulationCommand.id == cmd_id)
        )).scalar_one()
        assert row.status == "pending"          # server default
        assert row.payload["max_runtime"] == 60
        st = (await db.execute(select(SimulationProcessStatus))).scalar_one()
        assert st.state == "idle"
        # cleanup (shared session DB)
        await db.delete(row)
        await db.delete(st)
        for a in (await db.execute(select(AdminAuditEvent))).scalars():
            await db.delete(a)
        await db.commit()


@pytest.mark.asyncio
async def test_claim_pending_filters_by_command_and_blocks_a_concurrent_claim(engine):
    """claim_pending only returns a row matching `command`, and a second
    session claiming the same command while the first holds the row's
    FOR UPDATE lock (no commit yet) gets None — SKIP LOCKED, not a race."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as seed:
        # An older "stop" row must never be returned for command="start".
        stop_cmd = SimulationCommand(command="stop", payload=None)
        start_cmd = SimulationCommand(command="start", payload={"fresh": True})
        seed.add_all([stop_cmd, start_cmd])
        await seed.commit()
        stop_id, start_id = stop_cmd.id, start_cmd.id

    session_a = factory()
    session_b = factory()
    try:
        claimed = await claim_pending(session_a, command="start")
        assert claimed is not None
        assert claimed.id == start_id  # filtered by command, not the older stop row

        # Second session: the only pending "start" row is locked by session_a's
        # still-open (uncommitted) transaction, so SKIP LOCKED yields nothing.
        second = await claim_pending(session_b, command="start")
        assert second is None

        await finish_command(session_a, claimed.id, status="done", result="started run x")

        # Now that session_a committed, the row is claimable again.
        reclaimed = await claim_pending(session_b, command="start")
        assert reclaimed is None  # it is "done" now, no longer pending
    finally:
        await session_a.rollback()
        await session_a.close()
        await session_b.rollback()
        await session_b.close()
        async with factory() as cleanup:
            for row_id in (stop_id, start_id):
                row = await cleanup.get(SimulationCommand, row_id)
                if row is not None:
                    await cleanup.delete(row)
            await cleanup.commit()


@pytest.mark.asyncio
async def test_mark_pending_stale_flips_pending_only_and_is_command_scoped(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        start_pending = SimulationCommand(command="start", payload=None)
        stop_pending = SimulationCommand(command="stop", payload=None)
        start_done = SimulationCommand(command="start", payload=None, status="done")
        stop_failed = SimulationCommand(command="stop", payload=None, status="failed")
        db.add_all([start_pending, stop_pending, start_done, stop_failed])
        await db.commit()
        ids = {
            "start_pending": start_pending.id,
            "stop_pending": stop_pending.id,
            "start_done": start_done.id,
            "stop_failed": stop_failed.id,
        }

    async with factory() as db:
        count = await mark_pending_stale(db, reason="supervisor boot")
        assert count == 2

    async with factory() as db:
        sp = await db.get(SimulationCommand, ids["start_pending"])
        tp = await db.get(SimulationCommand, ids["stop_pending"])
        sd = await db.get(SimulationCommand, ids["start_done"])
        tf = await db.get(SimulationCommand, ids["stop_failed"])
        assert sp.status == "stale" and sp.result == "supervisor boot"
        assert tp.status == "stale" and tp.result == "supervisor boot"
        assert sd.status == "done"  # untouched
        assert tf.status == "failed"  # untouched
        for row in (sp, tp, sd, tf):
            await db.delete(row)
        await db.commit()

    # command-scoped: only the named kind is stale-d, the other stays pending
    async with factory() as db:
        start2 = SimulationCommand(command="start", payload=None)
        stop2 = SimulationCommand(command="stop", payload=None)
        db.add_all([start2, stop2])
        await db.commit()
        start2_id, stop2_id = start2.id, stop2.id

    async with factory() as db:
        count2 = await mark_pending_stale(db, reason="fresh run started", command="start")
        assert count2 == 1

    async with factory() as db:
        a = await db.get(SimulationCommand, start2_id)
        b = await db.get(SimulationCommand, stop2_id)
        assert a.status == "stale" and a.result == "fresh run started"
        assert b.status == "pending"  # a pending stop is left alone
        await db.delete(a)
        await db.delete(b)
        await db.commit()


@pytest.mark.asyncio
async def test_enqueue_command_duplicate_pending_same_kind_raises_integrity_error(engine):
    """The 0042 partial unique index refuses a second pending row of the same
    kind, but a pending start and a pending stop coexist fine."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    start_id = stop_id = None
    try:
        async with factory() as db:
            cmd1 = await enqueue_command(
                db, command="start", payload=None, requested_by_user_id=None
            )
            start_id = cmd1.id

        async with factory() as db:
            with pytest.raises(IntegrityError):
                await enqueue_command(
                    db, command="start", payload=None, requested_by_user_id=None
                )
            await db.rollback()

        async with factory() as db:
            cmd2 = await enqueue_command(
                db, command="stop", payload=None, requested_by_user_id=None
            )
            stop_id = cmd2.id
    finally:
        async with factory() as db:
            for row_id in (start_id, stop_id):
                if row_id is None:
                    continue
                row = await db.get(SimulationCommand, row_id)
                if row is not None:
                    await db.delete(row)
            await db.commit()


@pytest.mark.asyncio
async def test_upsert_status_twice_leaves_one_row_with_a_later_updated_at(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        # Guard against a leftover singleton row from an earlier failed run.
        existing = await read_status(db)
        if existing is not None:
            await db.delete(existing)
            await db.commit()

    async with factory() as db:
        await upsert_status(db, state="running", simulation_run_id=None, detail={"n": 1})

    async with factory() as db:
        first = await read_status(db)
        assert first is not None
        assert first.state == "running"
        first_updated_at = first.updated_at

    await asyncio.sleep(0.01)

    async with factory() as db:
        await upsert_status(db, state="stopped", simulation_run_id=None, detail={"n": 2})

    async with factory() as db:
        rows = (await db.execute(select(SimulationProcessStatus))).scalars().all()
        assert len(rows) == 1
        second = rows[0]
        assert second.id == 1
        assert second.state == "stopped"
        assert second.detail == {"n": 2}
        assert second.updated_at > first_updated_at
        await db.delete(second)
        await db.commit()


def test_derive_panel_state_not_deployed_when_row_is_absent():
    assert derive_panel_state(None, datetime.now(UTC)) == "not_deployed"


def test_derive_panel_state_stale_when_heartbeat_is_old():
    now = datetime.now(UTC)
    row = SimulationProcessStatus(state="running")
    row.updated_at = now - timedelta(seconds=HEARTBEAT_STALE_SECONDS + 1)
    assert derive_panel_state(row, now) == "stale"


def test_derive_panel_state_passes_through_state_when_heartbeat_is_fresh():
    now = datetime.now(UTC)
    row = SimulationProcessStatus(state="running")
    row.updated_at = now - timedelta(seconds=5)
    assert derive_panel_state(row, now) == "running"
