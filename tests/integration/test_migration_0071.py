"""0071: hub_declines; its columns, the one-row-per-interview unique constraint, the
reason-length CHECK, the run cascade under a raw DELETE FROM simulation_runs, and the
downgrade. The database is at head."""
import importlib.util
import sys
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.models.hub_decline import REASON_MAX_CHARS
from tests import factories

pytestmark = pytest.mark.integration
_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
CONSTRAINT_NAMES = {"uq_hub_declines_run_thread", "ck_hub_declines_reason_length"}
COLUMNS = {
    "id": ("uuid", "NO", None),
    "simulation_run_id": ("uuid", "NO", None),
    "agent_id": ("character varying", "NO", 50),
    "subject_agent_id": ("character varying", "YES", 50),
    "channel_name": ("character varying", "NO", 100),
    "thread_id": ("character varying", "NO", 50),
    "slack_ts": ("character varying", "YES", 50),
    "reason_text": ("text", "NO", None),
    "created_at": ("timestamp with time zone", "NO", None),
    "summary_claimed_at": ("timestamp with time zone", "YES", None),
    "summary_posted_at": ("timestamp with time zone", "YES", None),
}


def _m0071():
    spec = importlib.util.spec_from_file_location("_m0071", _VERSIONS / "0071_hub_declines.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _run(db_session, name: str) -> None:
    mod = _m0071()
    conn = await db_session.connection()

    def _call(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(mod, name)()

    await conn.run_sync(_call)


async def _columns(db_session) -> dict[str, tuple[str, str, int | None]]:
    rows = (await db_session.execute(text(
        "SELECT column_name, data_type, is_nullable, character_maximum_length "
        "FROM information_schema.columns WHERE table_name = 'hub_declines'"
    ))).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


async def _insert(db_session, run_id, *, thread_id="1600000800.000100", reason="⏸️ Declining."):
    await db_session.execute(text(
        "INSERT INTO hub_declines "
        "(id, simulation_run_id, agent_id, subject_agent_id, channel_name, thread_id, "
        " slack_ts, reason_text) "
        "VALUES (gen_random_uuid(), :run, 'blackbird', 'hu', 'general', :thread, "
        " '1600000800.000200', :reason)"
    ), {"run": run_id, "thread": thread_id, "reason": reason})


async def _count(db_session, run_id) -> int:
    return (await db_session.execute(text(
        "SELECT count(*) FROM hub_declines WHERE simulation_run_id = :r"), {"r": run_id}
    )).scalar_one()


def test_the_revision_cap_equals_the_model_cap():
    assert _m0071().REASON_MAX_CHARS == REASON_MAX_CHARS


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
    assert "uq_hub_declines_run_thread" in str(excinfo.value)


async def test_reason_text_is_capped(db_session):
    run = await factories.make_simulation_run(db_session)
    await _insert(db_session, run.id, thread_id="t-max", reason="x" * REASON_MAX_CHARS)
    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await _insert(db_session, run.id, thread_id="t-over", reason="x" * 4001)
    assert "ck_hub_declines_reason_length" in str(excinfo.value)


async def test_defaults(db_session):
    run = await factories.make_simulation_run(db_session)
    await _insert(db_session, run.id)
    row = (await db_session.execute(text(
        "SELECT created_at IS NOT NULL, summary_claimed_at IS NULL, summary_posted_at IS NULL "
        "FROM hub_declines WHERE simulation_run_id = :r"), {"r": run.id})).one()
    assert tuple(row) == (True, True, True)


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
        "AND conrelid = 'hub_declines'::regclass"))).scalars())
    assert names == CONSTRAINT_NAMES
