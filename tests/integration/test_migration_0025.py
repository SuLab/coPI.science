"""Integration test for migration 0025: publications
(user_id, pmid) dedup + unique constraint.

Uses its own scratch database, stamped to 0024 and then upgraded, rather than the
shared session-scoped `engine` fixture every other integration test relies on
staying at head for the whole run.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_alembic(dsn: str, target: str) -> None:
    alembic = os.path.join(os.path.dirname(sys.executable), "alembic")
    env = {**os.environ, "DATABASE_URL": dsn}
    r = subprocess.run(
        [alembic, "upgrade", target],
        cwd=str(_REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, f"alembic upgrade {target} failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"


@pytest.fixture
async def scratch_db(pg_url):
    """A throwaway database on the same Postgres server as pg_url, dropped on teardown."""
    admin_url = make_url(pg_url)
    db_name = f"mig0025_{uuid.uuid4().hex[:12]}"
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    # NOTE: str(URL) masks the password ("***") in SQLAlchemy >=1.4/2.0; that would
    # make every connection to the scratch DB fail with InvalidPasswordError
    # regardless of migration state. render_as_string(hide_password=False) keeps
    # the real credentials.
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


async def test_0025_dedups_existing_rows_then_adds_unique_constraint(scratch_db):
    _run_alembic(scratch_db, "0024")

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    user_id = uuid.uuid4()
    lower_id, higher_id = sorted([uuid.uuid4(), uuid.uuid4()])
    null_pmid_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    # is_admin / email_notifications_enabled / onboarding_complete are
                    # NOT NULL with NO server default (0001_initial.py:33-35 used a
                    # client-side `default=`, which emits no DDL DEFAULT), so a raw
                    # INSERT must name them. access_status is NOT NULL too — 0010 drops
                    # its default (0010_access_gate_and_waitlist.py:50).
                    "INSERT INTO users (id, name, orcid, access_status, is_admin, "
                    "email_notifications_enabled, onboarding_complete) "
                    "VALUES (:id, 'Dup User', :orcid, 'allowed', false, true, true)"
                ),
                {"id": user_id, "orcid": f"0000-0000-0000-{uuid.uuid4().hex[:4]}"},
            )
            # Two publications sharing (user_id, pmid) — the exact shape produced
            # when an ORCID works list lists the same PMID twice (profile_pipeline.py:115).
            for pub_id, title in ((lower_id, "Kept (lowest id)"), (higher_id, "Dropped (dup)")):
                await conn.execute(
                    text(
                        "INSERT INTO publications (id, user_id, pmid, title) "
                        "VALUES (:id, :user_id, '111', :title)"
                    ),
                    {"id": pub_id, "user_id": user_id, "title": title},
                )
            # A NULL-pmid row must survive untouched (NULLs are not "duplicates").
            await conn.execute(
                text(
                    "INSERT INTO publications (id, user_id, pmid, title) "
                    "VALUES (:id, :user_id, NULL, 'No PMID')"
                ),
                {"id": null_pmid_id, "user_id": user_id},
            )
    finally:
        await engine.dispose()

    _run_alembic(scratch_db, "head")

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT id, title FROM publications WHERE user_id = :u AND pmid = '111'"),
                    {"u": user_id},
                )
            ).all()
            assert [r.id for r in rows] == [lower_id], "dedup must keep the LOWEST id"
            assert rows[0].title == "Kept (lowest id)"

            remaining_null = (
                await conn.execute(
                    text("SELECT count(*) FROM publications WHERE id = :id"), {"id": null_pmid_id}
                )
            ).scalar_one()
            assert remaining_null == 1, "NULL-pmid rows must not be treated as duplicates"

            with pytest.raises(IntegrityError, match="uq_publications_user_pmid"):
                async with conn.begin_nested():
                    await conn.execute(
                        text(
                            "INSERT INTO publications (id, user_id, pmid, title) "
                            "VALUES (:id, :user_id, '111', 'blocked')"
                        ),
                        {"id": uuid.uuid4(), "user_id": user_id},
                    )
    finally:
        await engine.dispose()


async def _insert_user(conn, user_id) -> None:
    await conn.execute(
        text(
            "INSERT INTO users (id, name, orcid, access_status, is_admin, "
            "email_notifications_enabled, onboarding_complete) "
            "VALUES (:id, 'Dup User', :orcid, 'allowed', false, true, true)"
        ),
        {"id": user_id, "orcid": f"0000-0000-0000-{uuid.uuid4().hex[:4]}"},
    )


async def test_0025_keeps_the_earliest_created_row_not_the_lowest_uuid(scratch_db):
    """Publication.id is uuid4, uncorrelated with insertion
    order, so ``p.id > p2.id`` can delete the richer, earlier row and keep a
    title-only row created later. Use a UUID pair whose ordering is the REVERSE of
    created_at ordering to prove the keeper is chosen by created_at, not id.
    """
    _run_alembic(scratch_db, "0024")

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    user_id = uuid.uuid4()
    rich_id = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")  # numerically HIGH
    bare_id = uuid.UUID("00000000-0000-0000-0000-000000000000")  # numerically LOW
    try:
        async with engine.begin() as conn:
            await _insert_user(conn, user_id)
            await conn.execute(
                text(
                    "INSERT INTO publications (id, user_id, pmid, title, abstract, "
                    "methods_text, doi, pmcid, journal, year, author_position, created_at) "
                    "VALUES (:id, :user_id, '222', 'Rich title', 'An abstract', "
                    "'Methods text', '10.1000/rich', 'PMC1', 'Nature', 2020, 'first', "
                    "now() - interval '10 days')"
                ),
                {"id": rich_id, "user_id": user_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO publications (id, user_id, pmid, title, created_at) "
                    "VALUES (:id, :user_id, '222', 'Bare title', now())"
                ),
                {"id": bare_id, "user_id": user_id},
            )
    finally:
        await engine.dispose()

    _run_alembic(scratch_db, "head")

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, title, abstract, methods_text, doi, pmcid, journal, "
                        "year, author_position FROM publications "
                        "WHERE user_id = :u AND pmid = '222'"
                    ),
                    {"u": user_id},
                )
            ).all()
            assert len(rows) == 1, "duplicate must be collapsed to one row"
            survivor = rows[0]
            assert survivor.id == rich_id, (
                "the EARLIEST-created row must survive, not the numerically lowest uuid"
            )
            assert survivor.title == "Rich title"
            assert survivor.abstract == "An abstract"
            assert survivor.methods_text == "Methods text"
            assert survivor.doi == "10.1000/rich"
            assert survivor.pmcid == "PMC1"
            assert survivor.journal == "Nature"
            assert survivor.year == 2020
            assert survivor.author_position == "first"
    finally:
        await engine.dispose()


async def test_0025_merges_doomed_richer_row_into_an_earlier_sparse_keeper(scratch_db):
    """The keeper (earliest created_at) is kept by IDENTITY even when it is the
    sparser row; its nullable data columns must be COALESCE-filled from the doomed
    (later, richer) row before that row is deleted, so no data is lost either way.
    """
    _run_alembic(scratch_db, "0024")

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    user_id = uuid.uuid4()
    sparse_id = uuid.uuid4()
    rich_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await _insert_user(conn, user_id)
            await conn.execute(
                text(
                    "INSERT INTO publications (id, user_id, pmid, title, created_at) "
                    "VALUES (:id, :user_id, '333', 'Sparse title', now() - interval '10 days')"
                ),
                {"id": sparse_id, "user_id": user_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO publications (id, user_id, pmid, title, abstract, "
                    "methods_text, doi, pmcid, journal, year, author_position, created_at) "
                    "VALUES (:id, :user_id, '333', 'Rich title', 'An abstract', "
                    "'Methods text', '10.1000/rich2', 'PMC2', 'Cell', 2021, 'last', now())"
                ),
                {"id": rich_id, "user_id": user_id},
            )
    finally:
        await engine.dispose()

    _run_alembic(scratch_db, "head")

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, title, abstract, methods_text, doi, pmcid, journal, "
                        "year, author_position FROM publications "
                        "WHERE user_id = :u AND pmid = '333'"
                    ),
                    {"u": user_id},
                )
            ).all()
            assert len(rows) == 1, "duplicate must be collapsed to one row"
            survivor = rows[0]
            assert survivor.id == sparse_id, "identity must stay the earliest-created row"
            assert survivor.title == "Sparse title", "title is NOT NULL and must not be overwritten"
            assert survivor.abstract == "An abstract"
            assert survivor.methods_text == "Methods text"
            assert survivor.doi == "10.1000/rich2"
            assert survivor.pmcid == "PMC2"
            assert survivor.journal == "Cell"
            assert survivor.year == 2021
            assert survivor.author_position == "last"
    finally:
        await engine.dispose()
