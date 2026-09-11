import json
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
# check_publication_duplicates against a real DB
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
    check must WARN and name it, not silently pass."""
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


# --------------------------------------------------------------------------- #
# 0026 resolves the PCM user_id FK name from the catalog
# --------------------------------------------------------------------------- #


async def test_0026_resolves_a_renamed_fk_and_round_trips(scratch_db):
    """A differently-named constraint (create_all bootstrap, dump/restore rename,
    hand edit) must not abort `alembic upgrade` mid-window, and the downgrade must
    restore the original ondelete behaviour under the canonical name either way."""
    _run_alembic(scratch_db, "0025")

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "ALTER TABLE private_channel_members "
                    "RENAME CONSTRAINT private_channel_members_user_id_fkey "
                    "TO pcm_user_fkey_renamed"
                )
            )
    finally:
        await engine.dispose()

    _run_alembic(scratch_db, "head")  # must not abort even though the FK was renamed

    fk_def_sql = (
        "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conrelid = 'private_channel_members'::regclass AND contype = 'f' "
        "AND conname = 'private_channel_members_user_id_fkey'"
    )
    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text(fk_def_sql))).mappings().one()
            assert "ON DELETE CASCADE" in row["def"]
    finally:
        await engine.dispose()

    _run_alembic(scratch_db, "0025", cmd="downgrade")  # round-trip back past 0026

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text(fk_def_sql))).mappings().one()
            assert "ON DELETE SET NULL" in row["def"]
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- #
# postflight must verify WHICH rows 0025 deleted, not how many
#
# A count cannot detect a concurrent deleter working inside a duplicate group:
# every row it removes reduces 0025's own delete count by exactly one, so the net
# shrinkage still lands on the prediction. Demonstrated against a copy of
# production before this was fixed — 5 group keepers deleted mid-window, 0025 then
# deleted 218, total 223, and postflight reported "exactly the duplicates 0025 was
# measured to delete" with five rows of real data gone.
# --------------------------------------------------------------------------- #


async def _seed_duplicate_group(engine, pmid: str = "12345"):
    """One user with two publications sharing a pmid. Returns (user_id, keeper, doomed)
    with the keeper chosen the way 0025 chooses it: created_at ASC, id ASC."""
    user_id = uuid.uuid4()
    early, late = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, name, orcid, access_status, is_admin, "
                "email_notifications_enabled, onboarding_complete) "
                "VALUES (:id, 'Dup User', :orcid, 'allowed', false, true, true)"
            ),
            {"id": user_id, "orcid": f"0000-0000-0000-{uuid.uuid4().hex[:4]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO publications (id, user_id, pmid, title, created_at) VALUES "
                "(:keeper, :u, :pmid, 'keeper', now() - interval '1 day'), "
                "(:doomed, :u, :pmid, 'doomed', now())"
            ),
            {"keeper": early, "doomed": late, "u": user_id, "pmid": pmid},
        )
    return user_id, str(early), str(late)


async def _preflight_snapshot(scratch_db, tmp_path, target=pre.DEFAULT_TARGET):
    snap = tmp_path / f"snap_{uuid.uuid4().hex[:8]}.json"
    proc = subprocess.run(
        [
            sys.executable, str(_REPO_ROOT / "scripts" / "migrate" / "preflight.py"),
            "--database-url", scratch_db, "--target", target,
            "--snapshot", str(snap),
            "--backup-verified-elsewhere", "test",
        ],
        capture_output=True, text=True, timeout=300,
    )
    assert snap.is_file(), proc.stdout + proc.stderr
    return json.loads(snap.read_text()), snap


def _postflight(scratch_db, snap, target=pre.DEFAULT_TARGET, extra=()):
    return subprocess.run(
        [
            sys.executable, str(_REPO_ROOT / "scripts" / "migrate" / "postflight.py"),
            "--database-url", scratch_db, "--target", target, "--snapshot", str(snap),
            *extra,
        ],
        capture_output=True, text=True, timeout=300,
    )


async def test_preflight_names_the_rows_0025_will_delete_and_keep(scratch_db, tmp_path):
    _run_alembic(scratch_db, "0024")
    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        _user, keeper, doomed = await _seed_duplicate_group(engine)
    finally:
        await engine.dispose()

    payload, _ = await _preflight_snapshot(scratch_db, tmp_path)
    assert payload["expected_deletions"] == {"publications": 1}
    assert payload["expected_deleted_ids"]["publications"] == [doomed]
    assert payload["expected_kept_ids"]["publications"] == [keeper]


async def test_postflight_passes_when_0025_deleted_exactly_the_named_rows(scratch_db, tmp_path):
    _run_alembic(scratch_db, "0024")
    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        await _seed_duplicate_group(engine)
    finally:
        await engine.dispose()
    _payload, snap = await _preflight_snapshot(scratch_db, tmp_path)
    _run_alembic(scratch_db, "head")

    proc = _postflight(scratch_db, snap)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "exactly the 1 rows preflight named" in proc.stdout


async def test_postflight_catches_a_keeper_deleted_during_the_window(scratch_db, tmp_path):
    """The C1 attack: a concurrent process deletes the row 0025 would have KEPT. The
    net count still matches (0025 deletes one fewer), so only identity catches it."""
    _run_alembic(scratch_db, "0024")
    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        _user, keeper, _doomed = await _seed_duplicate_group(engine)
        _payload, snap = await _preflight_snapshot(scratch_db, tmp_path)
        async with engine.begin() as conn:      # the concurrent deleter
            await conn.execute(
                text("DELETE FROM publications WHERE id = :id"), {"id": keeper}
            )
    finally:
        await engine.dispose()
    _run_alembic(scratch_db, "head")

    proc = _postflight(scratch_db, snap)
    assert proc.returncode == 1, proc.stdout
    assert "supposed to KEEP are gone" in proc.stdout
    # and it must say so even when growth is being tolerated
    proc2 = _postflight(scratch_db, snap, extra=("--allow-row-growth",))
    assert proc2.returncode == 1, proc2.stdout
    assert "supposed to KEEP are gone" in proc2.stdout


async def test_postflight_refuses_a_snapshot_from_another_database(scratch_db, tmp_path):
    _run_alembic(scratch_db, "0024")
    _payload, snap = await _preflight_snapshot(scratch_db, tmp_path)
    payload = json.loads(snap.read_text())
    payload["database_url"] = "postgresql+asyncpg://copi:***@somewhere-else:5432/other"
    snap.write_text(json.dumps(payload))
    _run_alembic(scratch_db, "head")

    proc = _postflight(scratch_db, snap)
    assert proc.returncode == 1, proc.stdout
    assert "does not belong to this run" in proc.stdout


async def test_preflight_records_no_deletion_licence_when_0025_is_not_pending(scratch_db, tmp_path):
    """An expectation recorded for a chain that will not run 0025 would license
    arbitrary deletions of that many rows."""
    _run_alembic(scratch_db, "0024")
    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        await _seed_duplicate_group(engine)
    finally:
        await engine.dispose()

    payload, _ = await _preflight_snapshot(scratch_db, tmp_path, target="0024")
    assert payload["expected_deletions"] == {}
    assert payload["expected_deleted_ids"] == {}
