"""0067: help_intro_dismissals; its via CHECK, the primary key, the cascade under a raw
DELETE FROM users, and the downgrade. The database is at head."""
import importlib.util
import sys
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.models.help import CHECK_NAME, VIA_DISMISS, VIA_FINISH, VIA_VALUES
from tests import factories

pytestmark = pytest.mark.integration
_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _m0067():
    spec = importlib.util.spec_from_file_location(
        "_m0067", _VERSIONS / "0067_help_intro_dismissals.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _run(db_session, name: str) -> None:
    mod = _m0067()
    conn = await db_session.connection()

    def _call(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(mod, name)()

    await conn.run_sync(_call)


async def _columns(db_session) -> dict[str, tuple[str, str, int | None]]:
    rows = (await db_session.execute(text(
        "SELECT column_name, data_type, is_nullable, character_maximum_length "
        "FROM information_schema.columns WHERE table_name = 'help_intro_dismissals'"
    ))).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


async def _insert(db_session, user_id, via: str) -> None:
    await db_session.execute(text(
        "INSERT INTO help_intro_dismissals (user_id, via) VALUES (:u, :v)"),
        {"u": user_id, "v": via})


async def _count(db_session, user_id) -> int:
    return (await db_session.execute(text(
        "SELECT count(*) FROM help_intro_dismissals WHERE user_id = :u"), {"u": user_id}
    )).scalar_one()


def test_the_revision_values_equal_the_model_values():
    assert tuple(_m0067().VIA_VALUES) == VIA_VALUES


async def test_the_table_and_its_columns_exist(db_session):
    assert await _columns(db_session) == {
        "user_id": ("uuid", "NO", None),
        "dismissed_at": ("timestamp with time zone", "NO", None),
        "via": ("character varying", "NO", 16),
    }
    primary_key = (await db_session.execute(text(
        "SELECT a.attname FROM pg_constraint c "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
        "WHERE c.conrelid = 'help_intro_dismissals'::regclass AND c.contype = 'p'"
    ))).scalars().all()
    assert primary_key == ["user_id"]


async def test_both_via_values_are_admitted_and_dismissed_at_defaults(db_session):
    first = await factories.make_user(db_session)
    second = await factories.make_user(db_session)
    await _insert(db_session, first.id, VIA_DISMISS)
    await _insert(db_session, second.id, VIA_FINISH)
    stamped = (await db_session.execute(text(
        "SELECT count(*) FROM help_intro_dismissals "
        "WHERE user_id IN (:a, :b) AND dismissed_at IS NOT NULL"),
        {"a": first.id, "b": second.id})).scalar_one()
    assert stamped == 2


async def test_the_via_check_refuses_anything_else(db_session):
    user = await factories.make_user(db_session)
    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await _insert(db_session, user.id, "other")
    assert CHECK_NAME in str(excinfo.value)


async def test_one_row_per_user(db_session):
    user = await factories.make_user(db_session)
    await _insert(db_session, user.id, VIA_DISMISS)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _insert(db_session, user.id, VIA_FINISH)


async def test_raw_user_delete_cascades_the_row(db_session):
    user = await factories.make_user(db_session)
    await _insert(db_session, user.id, VIA_FINISH)
    assert await _count(db_session, user.id) == 1
    await db_session.execute(text("DELETE FROM users WHERE id = :u"), {"u": user.id})
    assert await _count(db_session, user.id) == 0


async def test_downgrade_drops_the_table_and_upgrade_restores_it(db_session):
    await _run(db_session, "downgrade")
    assert await _columns(db_session) == {}
    await _run(db_session, "upgrade")
    assert set(await _columns(db_session)) == {"user_id", "dismissed_at", "via"}
    names = set((await db_session.execute(text(
        "SELECT conname FROM pg_constraint WHERE contype = 'c' AND conname = :n"),
        {"n": CHECK_NAME})).scalars())
    assert names == {CHECK_NAME}
