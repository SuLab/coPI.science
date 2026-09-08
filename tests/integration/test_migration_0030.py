"""Integration test for migration 0030 (RC-1 + RC-2, #20 audit 2026-09-08).

0030 carries two columns and one backfill:

  * ``agent_messages.sender_user_id`` — the ownership carrier
    ``SimulationEngine._agent_ids_owned_by_user`` resolves against (RC-1 /
    #20 COR-5): nullable UUID, FK ``users.id`` ON DELETE SET NULL, indexed.
    No backfill — there is no way to recover who wrote a pre-existing row.
  * ``pi_dm_messages.handled_at`` — the durable at-least-once marker
    ``SimulationEngine._poll_pi_dms_from_db`` reads instead of the in-memory
    ``_pi_dm_seen`` set (RC-2). BACKFILLED for existing inbound rows
    (``handled_at = created_at``) so a deploy of this fix does not re-run
    ``PIHandler.handle_dm`` against the DM channel's entire history; outbound
    rows are left NULL (the column is meaningless for them).

Like ``test_migration_0029.py``, this uses its own scratch database — stamped
at 0029, upgraded, then downgraded back — rather than the shared
session-scoped ``engine`` fixture, which stays at head for the whole run.
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

#: (table, column, data_type, is_nullable, column_default) — the same five-tuple
#: shape postflight.EXPECTED_COLUMNS pins, so a drift in one is visible in the other.
_NEW_COLUMNS = (
    ("agent_messages", "sender_user_id", "uuid", "YES", None),
    ("pi_dm_messages", "handled_at", "timestamp with time zone", "YES", None),
)


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
    db_name = f"mig0030_{uuid.uuid4().hex[:12]}"
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


async def _seed_pre_existing_rows(conn) -> dict:
    """One simulation_run + one human agent_message + one inbound and one
    outbound pi_dm_messages row, as a pre-0030 deployment would already hold
    them. Both new columns must read NULL on the agent_message; the backfill
    must set handled_at on the inbound DM but not the outbound one."""
    run_id = uuid.uuid4()
    message_id = uuid.uuid4()
    inbound_dm_id = uuid.uuid4()
    outbound_dm_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO simulation_runs (id, started_at, status, total_messages, "
            "total_api_calls, config) VALUES (:id, now(), 'running', 0, 0, '{}')"
        ),
        {"id": run_id},
    )
    await conn.execute(
        text(
            "INSERT INTO agent_messages (id, simulation_run_id, agent_id, channel_id, "
            "channel_name, message_ts, message_length, phase, visibility, content, "
            "sender_name, is_bot, posted_at) "
            "VALUES (:id, :run, NULL, 'C1', 'general', '1700000000.000200', 3, "
            "'new_post', 'public', 'yes', 'PI', false, 1700000000.0)"
        ),
        {"id": message_id, "run": run_id},
    )
    await conn.execute(
        text(
            "INSERT INTO pi_dm_messages (id, simulation_run_id, agent_id, pi_user_id, "
            "direction, content, sender_name, ts, posted_at) "
            "VALUES (:id, :run, 'su', 'local:pi-1', 'inbound', 'always cc me', 'PI', "
            "'1700000000.000300', 1700000000.0)"
        ),
        {"id": inbound_dm_id, "run": run_id},
    )
    await conn.execute(
        text(
            "INSERT INTO pi_dm_messages (id, simulation_run_id, agent_id, pi_user_id, "
            "direction, content, sender_name, ts, posted_at) "
            "VALUES (:id, :run, 'su', 'local:pi-1', 'outbound', 'noted', 'SuBot', "
            "'1700000000.000400', 1700000000.0)"
        ),
        {"id": outbound_dm_id, "run": run_id},
    )
    return {
        "run_id": run_id, "message_id": message_id,
        "inbound_dm_id": inbound_dm_id, "outbound_dm_id": outbound_dm_id,
    }


async def test_0030_adds_two_columns_backfills_handled_at_and_downgrades_back_out(
    scratch_db,
):
    """Pre-state, upgrade + backfill, post-state, downgrade, pre-state restored."""
    _run_alembic(scratch_db, "0029")

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            ids = await _seed_pre_existing_rows(conn)
            before_am = await _columns(conn, "agent_messages")
            before_dm = await _columns(conn, "pi_dm_messages")
            assert "sender_user_id" not in before_am
            assert "handled_at" not in before_dm
    finally:
        await engine.dispose()

    _run_alembic(scratch_db, "head")

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rev = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            assert rev == "0030"

            after = {
                "agent_messages": await _columns(conn, "agent_messages"),
                "pi_dm_messages": await _columns(conn, "pi_dm_messages"),
            }
            for table, column, data_type, nullable, default in _NEW_COLUMNS:
                assert column in after[table], f"0030 must add {table}.{column}"
                assert after[table][column] == (data_type, nullable, default), (
                    f"{table}.{column} must be {data_type}, nullable, with NO server "
                    f"default — got {after[table][column]}"
                )

            # sender_user_id: no backfill, pre-existing row reads NULL.
            sender = (await conn.execute(
                text("SELECT sender_user_id FROM agent_messages WHERE id = :id"),
                {"id": ids["message_id"]},
            )).scalar_one()
            assert sender is None

            # handled_at: backfilled to created_at for the pre-existing INBOUND
            # row, left NULL for the OUTBOUND row.
            inbound_handled, inbound_created = (await conn.execute(
                text(
                    "SELECT handled_at, created_at FROM pi_dm_messages WHERE id = :id"
                ),
                {"id": ids["inbound_dm_id"]},
            )).one()
            assert inbound_handled is not None, "the backfill must not skip the inbound row"
            assert inbound_handled == inbound_created

            outbound_handled = (await conn.execute(
                text("SELECT handled_at FROM pi_dm_messages WHERE id = :id"),
                {"id": ids["outbound_dm_id"]},
            )).scalar_one()
            assert outbound_handled is None, (
                "the backfill must not touch outbound rows — the column is "
                "meaningless for them"
            )

            # 0030 carries EXACTLY these two columns: nothing else on either
            # table moved, and no other table gained or lost a column.
            assert set(after["agent_messages"]) - set(before_am) == {"sender_user_id"}
            assert set(after["pi_dm_messages"]) - set(before_dm) == {"handled_at"}
            assert set(before_am) - set(after["agent_messages"]) == set()
            assert set(before_dm) - set(after["pi_dm_messages"]) == set()

            # sender_user_id is a real FK to users, ON DELETE SET NULL.
            fk = (await conn.execute(
                text(
                    "SELECT confdeltype FROM pg_constraint "
                    "WHERE conrelid = 'agent_messages'::regclass "
                    "AND conname = 'agent_messages_sender_user_id_fkey'"
                )
            )).scalar_one()
            # confdeltype is Postgres "char" type; asyncpg returns it as bytes.
            fk_char = fk.decode() if isinstance(fk, (bytes, bytearray)) else fk
            assert fk_char == "n", f"expected ON DELETE SET NULL ('n'), got {fk!r}"

            # Indexed.
            idx = (await conn.execute(
                text(
                    "SELECT count(*) FROM pg_indexes WHERE tablename = 'agent_messages' "
                    "AND indexname = 'ix_agent_messages_sender_user_id'"
                )
            )).scalar_one()
            assert idx == 1
    finally:
        await engine.dispose()

    _run_alembic(scratch_db, "0029", cmd="downgrade")

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rev = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            assert rev == "0029"
            assert await _columns(conn, "agent_messages") == before_am
            assert await _columns(conn, "pi_dm_messages") == before_dm
            # The rows themselves survive the round trip.
            assert (await conn.execute(
                text("SELECT count(*) FROM agent_messages WHERE id = :id"),
                {"id": ids["message_id"]},
            )).scalar_one() == 1
            assert (await conn.execute(
                text("SELECT count(*) FROM pi_dm_messages WHERE simulation_run_id = :run"),
                {"run": ids["run_id"]},
            )).scalar_one() == 2
    finally:
        await engine.dispose()


async def test_0030_downgrade_is_idempotent_when_the_columns_are_already_gone(scratch_db):
    """The 0022+ convention: downgrades are ``if_exists``-guarded, so a half-applied
    or hand-repaired database does not abort the downgrade mid stop-the-world window."""
    _run_alembic(scratch_db, "head")

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE agent_messages DROP COLUMN sender_user_id"))
            await conn.execute(text("ALTER TABLE pi_dm_messages DROP COLUMN handled_at"))
    finally:
        await engine.dispose()

    _run_alembic(scratch_db, "0029", cmd="downgrade")  # must not raise

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rev = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            assert rev == "0029"
    finally:
        await engine.dispose()


def test_0030_is_the_only_head():
    alembic = os.path.join(os.path.dirname(sys.executable), "alembic")
    r = subprocess.run(
        [alembic, "heads"], cwd=str(_REPO_ROOT), capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    heads = [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]
    assert heads == ["0030"], r.stdout
