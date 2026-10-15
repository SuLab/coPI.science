"""0070: authorship_claim_checks; its CHECKs, defaults, the run cascade under a raw
DELETE FROM simulation_runs, a run-less row, and the downgrade. The database is at head."""
import importlib.util
import sys
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.models.authorship_claim_check import MODES, OUTCOMES
from tests import factories

pytestmark = pytest.mark.integration
_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
CHECK_NAMES = {
    "ck_authorship_claim_checks_mode",
    "ck_authorship_claim_checks_outcome",
    "ck_authorship_claim_checks_excerpt_length",
}
COLUMNS = {
    "id": ("uuid", "NO", None),
    "simulation_run_id": ("uuid", "YES", None),
    "agent_id": ("character varying", "NO", 50),
    "thread_key": ("character varying", "NO", 64),
    "phase": ("character varying", "NO", 16),
    "mode": ("character varying", "NO", 16),
    "outcome": ("character varying", "NO", 16),
    "reasons": ("jsonb", "NO", None),
    "excerpt": ("text", "NO", None),
    "created_at": ("timestamp with time zone", "NO", None),
}


def _m0070():
    spec = importlib.util.spec_from_file_location(
        "_m0070", _VERSIONS / "0070_authorship_claim_checks.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _run(db_session, name: str) -> None:
    mod = _m0070()
    conn = await db_session.connection()

    def _call(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(mod, name)()

    await conn.run_sync(_call)


async def _columns(db_session) -> dict[str, tuple[str, str, int | None]]:
    rows = (await db_session.execute(text(
        "SELECT column_name, data_type, is_nullable, character_maximum_length "
        "FROM information_schema.columns WHERE table_name = 'authorship_claim_checks'"
    ))).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


async def _insert(db_session, *, run_id=None, mode="log", outcome="no_claim", excerpt="x"):
    await db_session.execute(text(
        "INSERT INTO authorship_claim_checks "
        "(id, simulation_run_id, agent_id, thread_key, phase, mode, outcome, excerpt) "
        "VALUES (gen_random_uuid(), :run, 'wang', 'phase5', 'new_post', :mode, :outcome, :excerpt)"
    ), {"run": run_id, "mode": mode, "outcome": outcome, "excerpt": excerpt})


async def _count(db_session, run_id) -> int:
    return (await db_session.execute(text(
        "SELECT count(*) FROM authorship_claim_checks WHERE simulation_run_id = :r"), {"r": run_id}
    )).scalar_one()


def test_the_revision_values_equal_the_model_values():
    mod = _m0070()
    assert (tuple(mod.MODES), tuple(mod.OUTCOMES)) == (MODES, OUTCOMES)


async def test_the_table_and_its_columns_exist(db_session):
    assert await _columns(db_session) == COLUMNS


async def test_unknown_mode_is_refused(db_session):
    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await _insert(db_session, mode="shadow")
    assert "ck_authorship_claim_checks_mode" in str(excinfo.value)


async def test_unknown_outcome_is_refused(db_session):
    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await _insert(db_session, outcome="blocked")
    assert "ck_authorship_claim_checks_outcome" in str(excinfo.value)


async def test_excerpt_is_capped_at_2000(db_session):
    await _insert(db_session, excerpt="x" * 2000)
    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await _insert(db_session, excerpt="x" * 2001)
    assert "ck_authorship_claim_checks_excerpt_length" in str(excinfo.value)


async def test_defaults(db_session):
    run = await factories.make_simulation_run(db_session)
    await _insert(db_session, run_id=run.id)
    row = (await db_session.execute(text(
        "SELECT reasons = '[]'::jsonb, created_at IS NOT NULL FROM authorship_claim_checks "
        "WHERE simulation_run_id = :r"), {"r": run.id})).one()
    assert tuple(row) == (True, True)


async def test_raw_run_delete_cascades_the_rows(db_session):
    run = await factories.make_simulation_run(db_session)
    await _insert(db_session, run_id=run.id)
    await _insert(db_session, run_id=run.id, outcome="rejected")
    assert await _count(db_session, run.id) == 2
    await db_session.execute(text("DELETE FROM simulation_runs WHERE id = :id"), {"id": run.id})
    assert await _count(db_session, run.id) == 0


async def test_a_row_without_a_run_is_admitted(db_session):
    await _insert(db_session, run_id=None, mode="replay")
    n = (await db_session.execute(text(
        "SELECT count(*) FROM authorship_claim_checks "
        "WHERE simulation_run_id IS NULL AND mode = 'replay'"))).scalar_one()
    assert n >= 1


async def test_downgrade_drops_and_upgrade_restores(db_session):
    await _run(db_session, "downgrade")
    assert await _columns(db_session) == {}
    await _run(db_session, "upgrade")
    assert await _columns(db_session) == COLUMNS
    names = set((await db_session.execute(text(
        "SELECT conname FROM pg_constraint WHERE contype = 'c' "
        "AND conrelid = 'authorship_claim_checks'::regclass"))).scalars())
    assert names == CHECK_NAMES
