"""0058: the gate-reason column, pi_companies with its constraints, and the
company_discovery job type under the rebuilt one-active-per-user index; the downgrade
restores the 0057 shape except the enum value (Postgres has no DROP VALUE). The
testcontainers database is migrated to head by tests/conftest.py."""

import importlib.util
import sys
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from src.models import Job
from src.models.job import ONE_ACTIVE_PER_USER_TYPE_WHERE, PER_USER_JOB_TYPES
from src.services.job_queue import insert_job_if_absent
from tests import factories

pytestmark = pytest.mark.integration

_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
_CONSTRAINTS = {
    "uq_pi_companies_user_normalized_name", "ck_pi_companies_pi_role",
    "ck_pi_companies_funding_usd", "ck_pi_companies_status", "ck_pi_companies_origin",
}
_INSERT = text(
    "INSERT INTO pi_companies (id, user_id, company_name, normalized_name, pi_role, "
    "funding_usd, source_url, status, origin) VALUES (gen_random_uuid(), :u, :name, :norm, "
    ":role, :usd, 'https://example.org/x', :status, :origin)"
)


def _load(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _VERSIONS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _m0058():
    return _load("0058_hub_1_10_gate_reasons_and_pi_companies.py", "_m0058")


async def _run(db_session, name: str) -> None:
    """Run 0058's upgrade or downgrade on the session's own connection, so the test's
    outer transaction rolls every statement back."""
    mod = _m0058()
    conn = await db_session.connection()

    def _call(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(mod, name)()

    await conn.run_sync(_call)


async def _one_active_indexdef(db_session) -> str:
    return (await db_session.execute(text(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_jobs_one_active_per_user_type'"
    ))).scalar_one()


async def _job_type_labels(db_session) -> list[str]:
    return list((await db_session.execute(text(
        "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
        "WHERE t.typname = 'job_type_enum'"
    ))).scalars())


async def _gating_rationales_column(db_session):
    return (await db_session.execute(text(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'opportunity_assessments' AND column_name = 'gating_rationales'"
    ))).first()


def test_the_rebuilt_predicate_is_the_model_constant():
    assert _m0058().ONE_ACTIVE_WHERE == ONE_ACTIVE_PER_USER_TYPE_WHERE


def test_the_downgrade_restores_0056s_predicate_exactly():
    m0056 = _load("0056_phase3_constraints_and_indexes.py", "_m0056_from_0058")
    assert _m0058().ONE_ACTIVE_WHERE_0056 == m0056.ONE_ACTIVE_WHERE


def test_company_discovery_is_a_per_user_job_type():
    assert PER_USER_JOB_TYPES == (
        "generate_profile", "enrich_grants", "industry_evidence", "company_discovery")
    assert "company_discovery" in Job.type.type.enums


async def test_upgrade_shape(db_session):
    assert "company_discovery" in await _job_type_labels(db_session)
    assert "company_discovery" in await _one_active_indexdef(db_session)
    assert tuple(await _gating_rationales_column(db_session)) == ("jsonb", "YES")
    names = set((await db_session.execute(text(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'pi_companies'::regclass"
    ))).scalars())
    assert _CONSTRAINTS <= names
    indexes = set((await db_session.execute(text(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'pi_companies'"
    ))).scalars())
    assert "ix_pi_companies_user_id" in indexes


async def test_one_active_discovery_job_per_pi(db_session):
    user = await factories.make_user(db_session)
    db_session.add(Job(type="company_discovery", user_id=user.id, payload={}))
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(Job(type="company_discovery", user_id=user.id, payload={}))
            await db_session.flush()


async def test_a_finished_discovery_job_does_not_block_the_next(db_session):
    user = await factories.make_user(db_session)
    db_session.add(Job(type="company_discovery", user_id=user.id, payload={}, status="completed"))
    await db_session.flush()
    assert await insert_job_if_absent(
        db_session, type="company_discovery", user_id=user.id, payload={}) is not None


async def test_a_second_enqueue_inserts_nothing_and_raises_the_priority(db_session):
    """ON CONFLICT infers the rebuilt index only when the model's predicate matches it."""
    user = await factories.make_user(db_session)
    first = await insert_job_if_absent(
        db_session, type="company_discovery", user_id=user.id, payload={}, priority=-10)
    second = await insert_job_if_absent(
        db_session, type="company_discovery", user_id=user.id, payload={}, priority=10)
    assert first is not None and second is None
    count = await db_session.scalar(select(func.count()).select_from(Job).where(
        Job.user_id == user.id, Job.type == "company_discovery"))
    assert count == 1
    job = await db_session.get(Job, first)
    await db_session.refresh(job)
    assert job.priority == 10


@pytest.mark.parametrize("override", [
    {"role": "ceo"}, {"usd": -1}, {"status": "pending"}, {"origin": "imported"},
])
async def test_the_checks_refuse_bad_values(db_session, override):
    user = await factories.make_user(db_session)
    params = {"u": user.id, "name": "Acme", "norm": "acme", "role": "founder", "usd": None,
              "status": "confirmed", "origin": "manual", **override}
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(_INSERT, params)


async def test_a_normalized_name_is_unique_per_pi_only(db_session):
    one = await factories.make_user(db_session)
    two = await factories.make_user(db_session)
    base = {"name": "Acme", "norm": "acme", "role": "founder", "usd": 0,
            "status": "suggested", "origin": "discovered"}
    await db_session.execute(_INSERT, {"u": one.id, **base})
    await db_session.execute(_INSERT, {"u": two.id, **base})
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(_INSERT, {"u": one.id, **base, "name": "ACME Inc."})


async def test_the_rows_go_with_the_pi(db_session):
    user = await factories.make_user(db_session)
    await db_session.execute(_INSERT, {"u": user.id, "name": "Acme", "norm": "acme",
                                       "role": "founder", "usd": None, "status": "confirmed",
                                       "origin": "manual"})
    await db_session.execute(text("DELETE FROM users WHERE id = :u"), {"u": user.id})
    left = await db_session.scalar(
        text("SELECT count(*) FROM pi_companies WHERE user_id = :u"), {"u": user.id})
    assert left == 0


async def test_downgrade_restores_the_0057_shape_and_upgrade_reapplies(db_session):
    await _run(db_session, "downgrade")
    assert (await db_session.execute(text("SELECT to_regclass('pi_companies')"))).scalar() is None
    assert await _gating_rationales_column(db_session) is None
    indexdef = await _one_active_indexdef(db_session)
    assert "industry_evidence" in indexdef and "company_discovery" not in indexdef
    assert "company_discovery" in await _job_type_labels(db_session)   # no DROP VALUE

    await _run(db_session, "upgrade")
    assert (await db_session.execute(text("SELECT to_regclass('pi_companies')"))).scalar() is not None
    assert "company_discovery" in await _one_active_indexdef(db_session)
    assert tuple(await _gating_rationales_column(db_session)) == ("jsonb", "YES")
