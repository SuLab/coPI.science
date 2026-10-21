"""Integration test for migration 0031.

0031 adds ``users.session_epoch`` (nullable INTEGER, no default, no backfill) and the
``user_microsoft_identities`` table (one link per user, one user per Microsoft account,
FK ``users.id`` ON DELETE CASCADE).

Like ``test_migration_0030.py``, this uses its own scratch database, stamped at 0030,
upgraded, then downgraded back, rather than the shared session-scoped ``engine`` fixture.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: column -> (data_type, is_nullable, column_default) for user_microsoft_identities.
_TABLE_COLUMNS = {
    "id": ("uuid", "NO", None),
    "user_id": ("uuid", "NO", None),
    "tenant_id": ("uuid", "NO", None),
    "object_id": ("uuid", "NO", None),
    "sign_in_name": ("character varying", "YES", None),
    "linked_at": ("timestamp with time zone", "NO", "now()"),
    "last_sign_in_at": ("timestamp with time zone", "YES", None),
}


def _run_alembic(dsn: str, target: str, cmd: str = "upgrade") -> None:
    alembic = os.path.join(os.path.dirname(sys.executable), "alembic")
    env = {**os.environ, "DATABASE_URL": dsn}
    r = subprocess.run(
        [alembic, cmd, target],
        cwd=str(_REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, (
        f"alembic {cmd} {target} failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )


async def _columns(conn, table: str) -> dict[str, tuple[str, str, str | None]]:
    rows = (await conn.execute(
        text(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": table},
    )).all()
    return {r.column_name: (r.data_type, r.is_nullable, r.column_default) for r in rows}


@pytest.fixture
async def scratch_db(pg_url):
    """A throwaway database on the same Postgres server as pg_url, dropped on teardown."""
    admin_url = make_url(pg_url)
    db_name = f"mig0031_{uuid.uuid4().hex[:12]}"
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    # render_as_string(hide_password=False): str(URL) masks the password as "***",
    # which would make every connection to the scratch DB fail on credentials
    # regardless of migration state (see test_migration_0025.py).
    scratch_url = admin_url.set(database=db_name).render_as_string(hide_password=False)
    yield scratch_url
    async with admin_engine.connect() as conn:
        await conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :n AND pid <> pg_backend_pid()"
            ),
            {"n": db_name},
        )
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    await admin_engine.dispose()


async def _seed_user(conn) -> uuid.UUID:
    user_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO users (id, name, orcid, access_status, is_admin, "
            "email_notifications_enabled, onboarding_complete) "
            "VALUES (:id, 'Pre User', :orcid, 'allowed', false, true, true)"
        ),
        {"id": user_id, "orcid": f"0000-0000-0000-{uuid.uuid4().hex[:4]}"},
    )
    return user_id


async def _constraints(conn) -> dict[str, tuple[str, str]]:
    rows = (await conn.execute(
        text(
            "SELECT conname, contype, pg_get_constraintdef(oid) AS def, confdeltype "
            "FROM pg_constraint WHERE conrelid = 'user_microsoft_identities'::regclass"
        )
    )).all()
    out = {}
    for r in rows:
        # confdeltype is Postgres "char" type; asyncpg returns it as bytes.
        deltype = r.confdeltype.decode() if isinstance(r.confdeltype, bytes) else r.confdeltype
        out[r.conname] = (r[2], deltype)
    return out


async def test_0031_upgrade_adds_the_column_and_the_table(scratch_db):
    _run_alembic(scratch_db, "0030")
    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            user_id = await _seed_user(conn)
            assert "session_epoch" not in await _columns(conn, "users")
    finally:
        await engine.dispose()

    _run_alembic(scratch_db, "0031")

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            users = await _columns(conn, "users")
            assert users["session_epoch"] == ("integer", "YES", None)
            epoch = (await conn.execute(
                text("SELECT session_epoch FROM users WHERE id = :id"), {"id": user_id}
            )).scalar_one()
            assert epoch is None

            assert await _columns(conn, "user_microsoft_identities") == _TABLE_COLUMNS

            cons = await _constraints(conn)
            assert cons["uq_user_microsoft_identities_user_id"][0] == "UNIQUE (user_id)"
            assert (
                cons["uq_user_microsoft_identities_tenant_object"][0]
                == "UNIQUE (tenant_id, object_id)"
            )
            fk_def, fk_del = cons["user_microsoft_identities_user_id_fkey"]
            assert fk_def == "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"
            assert fk_del == "c"
    finally:
        await engine.dispose()


async def test_0031_deleting_a_user_removes_their_link(scratch_db):
    _run_alembic(scratch_db, "0031")
    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            user_id = await _seed_user(conn)
            await conn.execute(
                text(
                    "INSERT INTO user_microsoft_identities (id, user_id, tenant_id, object_id) "
                    "VALUES (:id, :u, :t, :o)"
                ),
                {"id": uuid.uuid4(), "u": user_id, "t": uuid.uuid4(), "o": uuid.uuid4()},
            )
            await conn.execute(text("DELETE FROM users"))
            remaining = (await conn.execute(
                text("SELECT count(*) FROM user_microsoft_identities")
            )).scalar_one()
            assert remaining == 0
    finally:
        await engine.dispose()


async def test_0031_downgrade_restores_0030(scratch_db):
    _run_alembic(scratch_db, "0030")
    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            user_id = await _seed_user(conn)
            before = await _columns(conn, "users")
    finally:
        await engine.dispose()

    _run_alembic(scratch_db, "0031")
    _run_alembic(scratch_db, "0030", cmd="downgrade")

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rev = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            assert rev == "0030"
            assert await _columns(conn, "users") == before
            assert await _columns(conn, "user_microsoft_identities") == {}
            assert (await conn.execute(
                text("SELECT count(*) FROM users WHERE id = :id"), {"id": user_id}
            )).scalar_one() == 1
    finally:
        await engine.dispose()


async def test_0031_downgrade_is_idempotent(scratch_db):
    """The 0022+ convention: downgrades are ``if_exists``-guarded."""
    _run_alembic(scratch_db, "0031")
    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE user_microsoft_identities"))
            await conn.execute(text("ALTER TABLE users DROP COLUMN session_epoch"))
    finally:
        await engine.dispose()

    _run_alembic(scratch_db, "0030", cmd="downgrade")  # must not raise

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rev = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            assert rev == "0030"
    finally:
        await engine.dispose()


def test_0031_is_the_only_head():
    alembic = os.path.join(os.path.dirname(sys.executable), "alembic")
    r = subprocess.run(
        [alembic, "heads"], cwd=str(_REPO_ROOT), capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    heads = [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]
    assert heads == ["0031"], r.stdout
