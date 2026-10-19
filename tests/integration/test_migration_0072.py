"""0072: interview_trajectories; its columns, the one-row-per-interview unique constraint,
the status CHECK, the run cascade under a raw DELETE FROM simulation_runs, and the
downgrade. The database is at head."""
import importlib.util
import sys
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests import factories

pytestmark = pytest.mark.integration
_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
CONSTRAINT_NAMES = {"uq_interview_trajectories_run_thread", "ck_interview_trajectories_status"}
COLUMNS = {
    "id": ("uuid", "NO", None),
    "simulation_run_id": ("uuid", "NO", None),
    "thread_id": ("character varying", "NO", 50),
    "status": ("character varying", "NO", 12),
    "lines": ("jsonb", "YES", None),
    "error_code": ("character varying", "YES", 40),
    "attempts": ("integer", "NO", None),
    "model": ("character varying", "NO", 100),
    "served_by_model": ("character varying", "YES", 100),
    "prompt_sha256_12": ("character varying", "NO", 12),
    "posts_sha256_12": ("character varying", "NO", 12),
    "usage_by_model": ("jsonb", "YES", None),
    "latency_ms": ("integer", "YES", None),
    "created_at": ("timestamp with time zone", "NO", None),
    "updated_at": ("timestamp with time zone", "NO", None),
}


def _m0072():
    spec = importlib.util.spec_from_file_location(
        "_m0072", _VERSIONS / "0072_interview_trajectories.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _run(db_session, name: str) -> None:
    mod = _m0072()
    conn = await db_session.connection()

    def _call(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(mod, name)()

    await conn.run_sync(_call)


async def _columns(db_session) -> dict[str, tuple[str, str, int | None]]:
    rows = (await db_session.execute(text(
        "SELECT column_name, data_type, is_nullable, character_maximum_length "
        "FROM information_schema.columns WHERE table_name = 'interview_trajectories'"
    ))).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


async def _insert(db_session, run_id, *, thread_id="1600000800.000100", status="ready"):
    await db_session.execute(text(
        "INSERT INTO interview_trajectories "
        "(id, simulation_run_id, thread_id, status, model, prompt_sha256_12, posts_sha256_12) "
        "VALUES (gen_random_uuid(), :run, :thread, :status, 'm', 'aaaaaaaaaaaa', 'bbbbbbbbbbbb')"
    ), {"run": run_id, "thread": thread_id, "status": status})


async def _count(db_session, run_id) -> int:
    return (await db_session.execute(text(
        "SELECT count(*) FROM interview_trajectories WHERE simulation_run_id = :r"),
        {"r": run_id},
    )).scalar_one()


async def test_the_table_and_its_columns_exist(db_session):
    assert await _columns(db_session) == COLUMNS


async def test_one_row_per_run_and_thread(db_session):
    run = await factories.make_simulation_run(db_session)
    other = await factories.make_simulation_run(db_session)
    await _insert(db_session, run.id)
    await _insert(db_session, other.id)  # the same thread id in another run is admitted
    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await _insert(db_session, run.id)
    assert "uq_interview_trajectories_run_thread" in str(excinfo.value)


async def test_status_is_checked(db_session):
    run = await factories.make_simulation_run(db_session)
    await _insert(db_session, run.id, thread_id="t-ok", status="refused")
    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await _insert(db_session, run.id, thread_id="t-bad", status="done")
    assert "ck_interview_trajectories_status" in str(excinfo.value)


async def test_defaults(db_session):
    run = await factories.make_simulation_run(db_session)
    await _insert(db_session, run.id)
    row = (await db_session.execute(text(
        "SELECT attempts, created_at IS NOT NULL, updated_at IS NOT NULL, lines IS NULL "
        "FROM interview_trajectories WHERE simulation_run_id = :r"), {"r": run.id})).one()
    assert tuple(row) == (1, True, True, True)


async def test_raw_run_delete_cascades_the_rows(db_session):
    run = await factories.make_simulation_run(db_session)
    await _insert(db_session, run.id, thread_id="t1")
    await _insert(db_session, run.id, thread_id="t2")
    assert await _count(db_session, run.id) == 2
    await db_session.execute(text("DELETE FROM simulation_runs WHERE id = :id"), {"id": run.id})
    assert await _count(db_session, run.id) == 0


async def test_downgrade_drops_and_upgrade_restores(db_session):
    await _run(db_session, "downgrade")
    assert await _columns(db_session) == {}
    await _run(db_session, "upgrade")
    assert await _columns(db_session) == COLUMNS
    names = set((await db_session.execute(text(
        "SELECT conname FROM pg_constraint WHERE contype IN ('c', 'u') "
        "AND conrelid = 'interview_trajectories'::regclass"))).scalars())
    assert names == CONSTRAINT_NAMES
