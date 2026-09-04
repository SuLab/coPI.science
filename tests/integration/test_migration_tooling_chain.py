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

from scripts.migrate import postflight as post
from scripts.migrate import preflight as pre

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_alembic(dsn: str, target: str, cmd: str = "upgrade") -> None:
    alembic = os.path.join(os.path.dirname(sys.executable), "alembic")
    env = {**os.environ, "DATABASE_URL": dsn}
    r = subprocess.run(
        [alembic, cmd, target],
        cwd=str(_REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, f"alembic {cmd} {target} failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"


@pytest.fixture
async def scratch_db(pg_url):
    """A throwaway database on the same Postgres server as pg_url, dropped on teardown.

    Needed (rather than the shared session-scoped `engine` fixture) whenever a test
    must exercise a pre-head revision: `engine` is migrated to head, where 0025's
    unique constraint makes a duplicate (user_id, pmid) pair impossible to seed, and
    a renamed FK on private_channel_members has already been normalised by 0026.
    """
    admin_url = make_url(pg_url)
    db_name = f"migchain_{uuid.uuid4().hex[:12]}"
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
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


async def test_postflight_expected_indexes_exist_at_head(engine):
    q = text("select indexname, indexdef from pg_indexes where schemaname = 'public'")
    async with engine.connect() as conn:
        have = {r.indexname: r.indexdef for r in (await conn.execute(q)).all()}
    for name, tail in post.EXPECTED_INDEXES.items():
        assert name in have, name
        assert have[name].endswith(tail), (name, have[name])


async def test_postflight_expected_constraint_defs_at_head(engine):
    # Alias the table-name column `tbl`, not `t`: sqlalchemy.engine.Row has its own
    # legacy `.t` attribute (a `_tuple()` alias, see the 2.0 deprecation warning), which
    # shadows a same-named result column on attribute access and made `r.t` silently
    # return the whole row instead of `rel.relname`.
    q = text(
        "select con.conname n, rel.relname tbl, pg_get_constraintdef(con.oid) d "
        "from pg_constraint con join pg_class rel on rel.oid = con.conrelid "
        "join pg_namespace ns on ns.oid = con.connamespace where ns.nspname = 'public'"
    )
    async with engine.connect() as conn:
        live = {r.n: (r.tbl, r.d) for r in (await conn.execute(q)).all()}
    for name, (table, expect) in post.EXPECTED_CONSTRAINTS.items():
        assert name in live, name
        assert live[name][0] == table, (name, live[name])
        assert expect in live[name][1], (name, live[name][1])


# --------------------------------------------------------------------------- #
# check_publication_duplicates against a real DB (#22 I3)
# --------------------------------------------------------------------------- #


async def test_check_publication_duplicates_passes_at_head(engine):
    """At head, 0025's unique constraint makes a real duplicate impossible."""
    async with engine.connect() as conn:
        _title, status, detail, rem, _data = await pre.check_publication_duplicates(conn)
    assert status == pre.PASS
    assert rem == []
    assert "nothing to merge" in detail or "does not exist" in detail


async def test_check_publication_duplicates_warns_with_a_seeded_duplicate(scratch_db):
    """Before 0025 has run, a real duplicate (user_id, pmid) pair can exist; the
    check must WARN and name it, not silently pass (#22 I3's whole point)."""
    _run_alembic(scratch_db, "0024")

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    user_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, name, orcid, access_status, is_admin, "
                    "email_notifications_enabled, onboarding_complete) "
                    "VALUES (:id, 'Dup User', :orcid, 'allowed', false, true, true)"
                ),
                {"id": user_id, "orcid": f"0000-0000-0000-{uuid.uuid4().hex[:4]}"},
            )
            for pub_id in (uuid.uuid4(), uuid.uuid4()):
                await conn.execute(
                    text(
                        "INSERT INTO publications (id, user_id, pmid, title) "
                        "VALUES (:id, :user_id, '999', 'A title')"
                    ),
                    {"id": pub_id, "user_id": user_id},
                )

        async with engine.connect() as conn:
            title, status, detail, rem, data = await pre.check_publication_duplicates(conn)
        assert title == "Publication (user_id, pmid) duplicates 0025 will merge and delete"
        assert status == pre.WARN
        assert data == {"duplicate_groups": 1, "duplicate_rows": 2}
        assert f"({user_id}, 999)" in detail
        assert any("\\copy" in r for r in rem)
    finally:
        await engine.dispose()

