"""0073: the projects prototype's four tables, the ``project_closed`` thread outcome, the
one-project-per-slot unique constraint, the downgrade (which keeps the enum value), and what
a pre-0073 ORM mapping does with a ``project_closed`` decision. The database is at head, so
the downgrade test first takes 0076 and 0075 off: their foreign keys reference hub_projects."""
import importlib.util
import sys
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Enum, column, select, table, text
from sqlalchemy.exc import IntegrityError

from tests import factories

pytestmark = pytest.mark.integration
_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
TABLES = {"hub_projects", "hub_project_calls", "hub_project_clocks", "hub_project_reviews"}
#: Matched by ``_tables``' LIKE too; created by 0075.
LATER_TABLES = {"hub_project_review_assignments"}
#: The revisions above 0073 that reference hub_projects, newest first.
LATER = ("0076_project_chat.py", "0075_project_review_assignments.py")


def _module(filename: str):
    name = "_m" + filename.split("_", 1)[0]
    spec = importlib.util.spec_from_file_location(name, _VERSIONS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _run(db_session, name: str, filename: str = "0073_hub_projects.py") -> None:
    mod = _module(filename)
    conn = await db_session.connection()

    def _call(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(mod, name)()

    await conn.run_sync(_call)


async def _tables(db_session) -> set[str]:
    rows = (await db_session.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name LIKE 'hub_project%'"
    ))).scalars()
    return set(rows)


async def _outcomes(db_session) -> set[str]:
    rows = (await db_session.execute(text(
        "SELECT unnest(enum_range(NULL::thread_outcome_enum))::text"
    ))).scalars()
    return set(rows)


async def _insert_project(db_session, run_id, *, agent_id="lab1", slot=1):
    await db_session.execute(text(
        "INSERT INTO hub_projects "
        "(id, simulation_run_id, agent_id, slot, status, requested_run_seconds) "
        "VALUES (gen_random_uuid(), :run, :agent, :slot, 'requested', 0)"
    ), {"run": run_id, "agent": agent_id, "slot": slot})


async def test_upgrade_creates_the_four_tables_and_the_outcome(db_session):
    assert await _tables(db_session) == TABLES | LATER_TABLES
    assert "project_closed" in await _outcomes(db_session)


async def test_one_project_per_run_lab_and_slot(db_session):
    run = await factories.make_simulation_run(db_session)
    await _insert_project(db_session, run.id)
    await _insert_project(db_session, run.id, slot=2)
    await _insert_project(db_session, run.id, agent_id="lab2")
    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await _insert_project(db_session, run.id)
    assert "uq_hub_projects_run_agent_slot" in str(excinfo.value)


async def test_downgrade_drops_the_tables_keeps_the_enum_value_and_upgrade_restores(db_session):
    for later in LATER:
        await _run(db_session, "downgrade", later)
    await _run(db_session, "downgrade")
    assert await _tables(db_session) == set()
    assert "project_closed" in await _outcomes(db_session)
    await _run(db_session, "upgrade")
    assert await _tables(db_session) == TABLES
    assert "project_closed" in await _outcomes(db_session)
    for later in reversed(LATER):
        await _run(db_session, "upgrade", later)
    assert await _tables(db_session) == TABLES | LATER_TABLES


async def test_pre_0073_enum_mapping_cannot_read_project_closed(db_session):
    """The rollback hazard the 0073 deploy note states: an image whose ORM maps the
    pre-0073 outcome list cannot load a run's ``project_closed`` decision."""
    run = await factories.make_simulation_run(db_session)
    decision = await factories.make_thread_decision(db_session, run=run, outcome="project_closed")
    old_outcome = Enum(
        "proposal", "no_proposal", "timeout", "abandoned", name="thread_outcome_enum"
    )
    decisions = table("thread_decisions", column("id"), column("outcome", old_outcome))
    # Rows are converted as they are fetched; both steps sit in the block so the pin
    # holds whether execute() or scalar_one() does the conversion.
    with pytest.raises(LookupError, match="project_closed"):
        result = await db_session.execute(
            select(decisions.c.outcome).where(decisions.c.id == decision.id)
        )
        result.scalar_one()
