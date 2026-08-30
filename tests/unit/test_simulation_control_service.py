"""Control-plane rows: commands are one-shot and claimable exactly once;
the status row is a single upserted heartbeat; audit events are append-only."""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import AdminAuditEvent, SimulationCommand, SimulationProcessStatus

pytestmark = pytest.mark.asyncio


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
