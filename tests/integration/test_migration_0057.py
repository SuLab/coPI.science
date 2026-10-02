"""0057: both users columns exist after upgrade, the D8 backfill stamps exactly the users
that have an email, and the downgrade drops both. The testcontainers database is migrated
to head by tests/conftest.py; each step here runs on the test's own connection, so the
outer transaction rolls every DDL statement back."""
import importlib.util
import sys
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from tests import factories

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic/versions/0057_email_verification_and_session_epoch.py"
)
_COLUMNS = {"email_verified_at", "session_epoch"}


def _load_migration():
    spec = importlib.util.spec_from_file_location("_m0057", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _run_migration_step(db_session, name):
    mod = _load_migration()
    conn = await db_session.connection()

    def _call(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(mod, name)()

    await conn.run_sync(_call)


async def _user_columns(db_session) -> set[str]:
    return set((await db_session.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'users'"
    ))).scalars())


async def test_head_has_both_nullable_columns_without_defaults(db_session):
    rows = (await db_session.execute(text(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = 'users' AND column_name IN ('email_verified_at', 'session_epoch')"
    ))).all()
    assert sorted(tuple(r) for r in rows) == [
        ("email_verified_at", "timestamp with time zone", "YES", None),
        ("session_epoch", "integer", "YES", None),
    ]


async def test_downgrade_drops_and_upgrade_backfills_only_users_with_an_email(db_session):
    with_email = await factories.make_user(db_session, email="d8@example.edu")
    without_email = await factories.make_user(db_session, email=None)
    await db_session.flush()

    await _run_migration_step(db_session, "downgrade")
    assert not (await _user_columns(db_session)) & _COLUMNS

    await _run_migration_step(db_session, "upgrade")
    assert _COLUMNS <= await _user_columns(db_session)
    stamped = dict((await db_session.execute(text(
        "SELECT id, email_verified_at IS NOT NULL FROM users WHERE id IN (:a, :b)"
    ), {"a": with_email.id, "b": without_email.id})).all())
    assert stamped == {with_email.id: True, without_email.id: False}
    bumped = (await db_session.execute(text(
        "SELECT count(*) FROM users WHERE session_epoch IS NOT NULL"
    ))).scalar_one()
    assert bumped == 0, "session_epoch is never backfilled"
