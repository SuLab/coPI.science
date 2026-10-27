"""0074: the ``stopped`` thread outcome and the ``system`` simulation command, the downgrade
(which keeps both values), and both values through the ORM. The database is at head."""
import importlib.util
import sys
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text

from src.models import SimulationCommand, ThreadDecision
from tests import factories

pytestmark = pytest.mark.integration
_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _m0074():
    spec = importlib.util.spec_from_file_location(
        "_m0074", _VERSIONS / "0074_independent_controls.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _run(db_session, name: str) -> None:
    mod = _m0074()
    conn = await db_session.connection()

    def _call(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(mod, name)()

    await conn.run_sync(_call)


async def _values(db_session, enum_name: str) -> set[str]:
    rows = (await db_session.execute(text(
        f"SELECT unnest(enum_range(NULL::{enum_name}))::text"
    ))).scalars()
    return set(rows)


async def _assert_both_values(db_session) -> None:
    assert "stopped" in await _values(db_session, "thread_outcome_enum")
    assert "system" in await _values(db_session, "sim_command_enum")


async def test_upgrade_adds_both_values(db_session):
    await _assert_both_values(db_session)


async def test_downgrade_keeps_both_values_and_upgrade_again_succeeds(db_session):
    await _run(db_session, "downgrade")
    await _assert_both_values(db_session)
    await _run(db_session, "upgrade")
    await _assert_both_values(db_session)


async def test_stopped_decision_round_trips_through_the_orm(db_session):
    run = await factories.make_simulation_run(db_session)
    decision = await factories.make_thread_decision(db_session, run=run, outcome="stopped")
    db_session.expunge(decision)
    loaded = (await db_session.execute(
        select(ThreadDecision).where(ThreadDecision.id == decision.id)
    )).scalar_one()
    assert loaded.outcome == "stopped"


async def test_system_command_round_trips_through_the_orm(db_session):
    run = await factories.make_simulation_run(db_session)
    payload = {"action": "wind_down", "system": "projects", "run_id": str(run.id)}
    cmd = SimulationCommand(command="system", payload=payload)
    db_session.add(cmd)
    await db_session.flush()
    db_session.expunge(cmd)
    loaded = (await db_session.execute(
        select(SimulationCommand).where(SimulationCommand.id == cmd.id)
    )).scalar_one()
    assert loaded.command == "system"
    assert loaded.payload == payload
