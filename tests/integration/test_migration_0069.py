"""0069: profile_revisions.owner_user_id, its cascade under a raw DELETE FROM users, its
index, and the downgrade. The database is at head (spec 2026-10-15 §10 R5.17)."""
import importlib.util
import sys
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text

from src.models import ProfileRevision
from tests import factories

pytestmark = pytest.mark.integration
_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
FK = "fk_profile_revisions_owner_user_id_users"
INDEX = "ix_profile_revisions_owner_user_id"


def _m0069():
    spec = importlib.util.spec_from_file_location(
        "_m0069", _VERSIONS / "0069_profile_revision_owner.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _run(db_session, name: str) -> None:
    mod = _m0069()
    conn = await db_session.connection()

    def _call(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(mod, name)()

    await conn.run_sync(_call)


async def _column(db_session):
    row = (await db_session.execute(text(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'profile_revisions' AND column_name = 'owner_user_id'"
    ))).one_or_none()
    return tuple(row) if row is not None else None


async def _has_index(db_session) -> bool:
    return (await db_session.execute(text(
        "SELECT count(*) FROM pg_indexes WHERE indexname = :n"), {"n": INDEX})).scalar_one() == 1


async def test_0069_adds_nullable_owner_with_cascade_and_index(db_session):
    assert await _column(db_session) == ("uuid", "YES")
    assert (await db_session.execute(text(
        "SELECT confdeltype::text FROM pg_constraint WHERE conname = :n"), {"n": FK}
    )).scalar_one() == "c"
    assert await _has_index(db_session)
    pi = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=pi)
    await db_session.execute(text(
        "INSERT INTO profile_revisions (id, agent_registry_id, profile_type, content, mechanism) "
        "VALUES (gen_random_uuid(), :a, 'public', 'old code', 'pipeline')"), {"a": agent.id})
    owners = (await db_session.execute(select(ProfileRevision.owner_user_id).where(
        ProfileRevision.agent_registry_id == agent.id))).scalars().all()
    assert owners == [None]


async def test_0069_downgrade_removes_it(db_session):
    await _run(db_session, "downgrade")
    assert await _column(db_session) is None
    assert not await _has_index(db_session)
    await _run(db_session, "upgrade")
    assert await _column(db_session) == ("uuid", "YES")


async def test_user_delete_cascades_owned_revisions(db_session):
    former = await factories.make_user(db_session)
    owner = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=owner)
    db_session.add_all([
        ProfileRevision(agent_registry_id=agent.id, profile_type="public", content="former's",
                        mechanism="pipeline", owner_user_id=former.id),
        ProfileRevision(agent_registry_id=agent.id, profile_type="public", content="owner's",
                        mechanism="pipeline", owner_user_id=owner.id),
    ])
    await db_session.flush()
    await db_session.execute(text("DELETE FROM users WHERE id = :u"), {"u": former.id})
    left = (await db_session.execute(text(
        "SELECT content FROM profile_revisions WHERE agent_registry_id = :a"), {"a": agent.id}
    )).scalars().all()
    assert left == ["owner's"]
