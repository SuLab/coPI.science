"""0061: pi_industry_scores.coverage, company_discovery_usage and
company_discovery_coi_ledger; their FK rule (CASCADE on user_id) under a raw DELETE FROM
users; the ledger key is unique; the downgrade drops them. The database is at head."""
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.models.pi_company import COI_LEDGER_OUTCOMES, COI_USAGE_STATUSES
from tests import factories

pytestmark = pytest.mark.integration
_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _m0061():
    spec = importlib.util.spec_from_file_location(
        "_m0061", _VERSIONS / "0061_industry_coverage_discovery_budget.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _run(db_session, name: str) -> None:
    """Run 0061's upgrade or downgrade on the session's own connection, so the test's
    outer transaction rolls every statement back."""
    mod = _m0061()
    conn = await db_session.connection()

    def _call(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(mod, name)()

    await conn.run_sync(_call)


async def _columns(db_session, table: str) -> set[str]:
    return set((await db_session.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
    ), {"t": table})).scalars())


async def _usage(db_session, user_id, status="reserved"):
    await db_session.execute(text(
        "INSERT INTO company_discovery_usage (id, user_id, pmid, model, status, reserved_usd) "
        "VALUES (:i, :u, '1', 'claude-opus-5-5', :s, 0.30)"),
        {"i": uuid.uuid4(), "u": user_id, "s": status})


async def _ledger(db_session, user_id, outcome="ok"):
    await db_session.execute(text(
        "INSERT INTO company_discovery_coi_ledger "
        "(id, user_id, pmid, statement_hash, name_forms_hash, outcome, claims) "
        "VALUES (:i, :u, '1', 'a', 'b', :o, '[]'::jsonb)"),
        {"i": uuid.uuid4(), "u": user_id, "o": outcome})


def test_the_check_constraints_list_the_model_values():
    mod = _m0061()
    assert tuple(mod.USAGE_STATUSES) == COI_USAGE_STATUSES
    assert tuple(mod.LEDGER_OUTCOMES) == COI_LEDGER_OUTCOMES


async def test_raw_user_delete_cascades_both_tables(db_session):
    pi = await factories.make_user(db_session)
    await _usage(db_session, pi.id)
    await _ledger(db_session, pi.id)
    await db_session.execute(text("DELETE FROM users WHERE id = :u"), {"u": pi.id})
    for table in ("company_discovery_usage", "company_discovery_coi_ledger"):
        count = (await db_session.execute(text(
            f"SELECT count(*) FROM {table} WHERE user_id = :u"), {"u": pi.id})).scalar_one()
        assert count == 0, table


async def test_one_ledger_row_per_key_and_known_values_only(db_session):
    pi = await factories.make_user(db_session)
    await _ledger(db_session, pi.id)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _ledger(db_session, pi.id, outcome="skipped")
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(text(
                "INSERT INTO company_discovery_coi_ledger "
                "(id, user_id, pmid, statement_hash, name_forms_hash, outcome) "
                "VALUES (:i, :u, '2', 'a', 'b', 'maybe')"), {"i": uuid.uuid4(), "u": pi.id})
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _usage(db_session, pi.id, status="paid")


async def test_coverage_is_nullable_on_old_rows(db_session):
    pi = await factories.make_user(db_session)
    await db_session.execute(text(
        "INSERT INTO pi_industry_scores (id, user_id, components, evidence_count, scorer_version) "
        "VALUES (:i, :u, '{}'::jsonb, 0, '1.0.0')"), {"i": uuid.uuid4(), "u": pi.id})
    coverage = (await db_session.execute(text(
        "SELECT coverage FROM pi_industry_scores WHERE user_id = :u"), {"u": pi.id})).scalar_one()
    assert coverage is None


async def test_downgrade_drops_everything_and_upgrade_restores_it(db_session):
    await _run(db_session, "downgrade")
    assert "coverage" not in await _columns(db_session, "pi_industry_scores")
    assert await _columns(db_session, "company_discovery_usage") == set()
    assert await _columns(db_session, "company_discovery_coi_ledger") == set()
    await _run(db_session, "upgrade")
    assert "coverage" in await _columns(db_session, "pi_industry_scores")
    usage = await _columns(db_session, "company_discovery_usage")
    assert {"reserved_usd", "cost_usd", "usage_by_model"} <= usage
    ledger = await _columns(db_session, "company_discovery_coi_ledger")
    assert {"statement_hash", "name_forms_hash", "claims"} <= ledger
