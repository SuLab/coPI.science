"""0056: every object exists after upgrade, each unique object's precheck names its
offenders, and the downgrade leaves the 0055 shape. The testcontainers database is
migrated to head by tests/conftest.py."""
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.models import Job, RubricDocument, SlackAppProvision
from tests import factories

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic/versions/0056_phase3_constraints_and_indexes.py"
)

_OBJECTS = {
    "uq_jobs_one_active_per_user_type",
    "uq_slack_app_provisions_agent",
    "uq_publications_user_pmid",
    "uq_users_email_lower",
    "ix_agent_messages_agent_phase",
    "ix_chat_usage_streaming",
}


def _load_migration():
    spec = importlib.util.spec_from_file_location("_m0056", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _run_migration_step(db_session, name):
    """Run the migration's upgrade/downgrade/_precheck on the session's own
    connection, so the test's outer transaction rolls every DDL statement back."""
    mod = _load_migration()
    conn = await db_session.connection()

    def _call(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(mod, name)()

    await conn.run_sync(_call)


async def _index_names(db_session):
    return set((await db_session.execute(text(
        "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
    ))).scalars())


async def test_indexes_exist(db_session):
    assert _OBJECTS <= await _index_names(db_session)


async def test_priority_column_is_nullable_smallint(db_session):
    row = (await db_session.execute(text(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'jobs' AND column_name = 'priority'"
    ))).one()
    assert tuple(row) == ("smallint", "YES")


async def test_rubric_documents_table(db_session):
    db_session.add(RubricDocument(content_hash="abc123def456", sha256="0" * 64,
                                  version="3.5.0", toml="[meta]\n"))
    await db_session.flush()
    got = await db_session.get(RubricDocument, "abc123def456")
    await db_session.refresh(got)
    assert got.first_seen_at is not None


async def test_one_active_job_per_user_type(db_session):
    user = await factories.make_user(db_session)
    db_session.add(Job(type="generate_profile", user_id=user.id, payload={}))
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(Job(type="generate_profile", user_id=user.id, payload={}))
            await db_session.flush()


async def test_review_jobs_are_not_constrained(db_session):
    user = await factories.make_user(db_session)
    for _ in range(3):
        db_session.add(Job(type="review_feedback_analysis", user_id=user.id,
                           payload={"assessment_id": str(uuid.uuid4())}))
    await db_session.flush()


async def test_email_lower_unique(db_session):
    await factories.make_user(db_session, email="Case@Example.edu")
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await factories.make_user(db_session, email="case@example.edu")


async def test_downgrade_restores_the_0055_shape_and_upgrade_reapplies(db_session):
    await _run_migration_step(db_session, "downgrade")
    names = await _index_names(db_session)
    assert not (_OBJECTS & names)
    assert not (await db_session.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'jobs' AND column_name = 'priority'"
    ))).first()
    assert (await db_session.execute(text("SELECT to_regclass('rubric_documents')"))).scalar() is None
    await _run_migration_step(db_session, "upgrade")
    assert _OBJECTS <= await _index_names(db_session)


async def test_prechecks_name_every_offender(db_session):
    await _run_migration_step(db_session, "downgrade")
    user = await factories.make_user(db_session, email="Dup@Example.edu")
    await factories.make_user(db_session, email="dup@example.edu")
    agent = await factories.make_agent(db_session, user=user)
    for _ in range(2):
        await db_session.execute(text(
            "INSERT INTO jobs(id, type, status, user_id, payload, attempts, max_attempts, enqueued_at) "
            "VALUES (gen_random_uuid(), 'generate_profile', 'pending', :u, '{}', 0, 3, now())"
        ), {"u": user.id})
        await db_session.execute(text(
            "INSERT INTO publications(id, user_id, pmid, title, created_at) "
            "VALUES (gen_random_uuid(), :u, '12345', 't', now())"
        ), {"u": user.id})
    for n in range(2):
        db_session.add(SlackAppProvision(
            agent_registry_id=agent.id, state=f"s{uuid.uuid4().hex[:8]}{n}",
            client_id="cid", client_secret="secret",
        ))
    await db_session.flush()
    with pytest.raises(RuntimeError) as err:
        await _run_migration_step(db_session, "upgrade")
    msg = str(err.value)
    assert "nothing was changed" in msg
    for name in ("jobs:", "slack_app_provisions:", "publications:", "users.email case duplicates:"):
        assert name in msg
    assert "remediate_0056.py --jobs" in msg
