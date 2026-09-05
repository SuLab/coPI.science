"""Integration test for migration 0029 (#20 COR-5 + COR-10(3)).

0029 carries exactly two nullable columns and nothing else:

  * ``thread_decisions.pi_engaged_at`` — the carrier for the implicit PI review
    that ``proposal_reviews`` cannot hold (its ``user_id`` is
    ``ondelete="CASCADE"`` to ``users``, so a PI deletion would erase the
    engine's own block-clearing markers). See
    ``docs/plans/2026-09-04-decisions/task-8.md``.
  * ``agent_messages.pi_inbound_state`` — the durable handled-marker the DB
    inbound poller needs once the log append moves *ahead* of the handler, so
    dedup stops keying on the MessageLog entry's presence. See
    ``docs/plans/2026-09-04-decisions/task-7.md``.

Both are nullable with no server default and no backfill: NULL means "unknown",
and every reader must treat unknown as today's behaviour.

Like ``test_migration_0025.py``, this uses its own scratch database — explicitly
created, stamped at 0028, upgraded, then downgraded back — rather than the
shared session-scoped ``engine`` fixture, which stays at head for the whole run.
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
    ("thread_decisions", "pi_engaged_at", "timestamp with time zone", "YES", None),
    ("agent_messages", "pi_inbound_state", "character varying", "YES", None),
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
    db_name = f"mig0029_{uuid.uuid4().hex[:12]}"
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


async def _seed_run_with_one_thread_and_one_message(conn) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """One simulation_run + one thread_decision + one agent_message, as a pre-0029
    deployment would already hold them. Both new columns must read NULL on these."""
    run_id, decision_id, message_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO simulation_runs (id, started_at, status, total_messages, "
            "total_api_calls, config) VALUES (:id, now(), 'running', 0, 0, '{}')"
        ),
        {"id": run_id},
    )
    await conn.execute(
        text(
            "INSERT INTO thread_decisions (id, simulation_run_id, thread_id, channel, "
            "agent_a, agent_b, outcome, origin_visibility, decided_at) "
            "VALUES (:id, :run, '1700000000.000100', 'collab-a', 'alpha', 'beta', "
            "'proposal', 'public', now())"
        ),
        {"id": decision_id, "run": run_id},
    )
    await conn.execute(
        text(
            "INSERT INTO agent_messages (id, simulation_run_id, agent_id, channel_id, "
            "channel_name, message_ts, message_length, phase, visibility, content, "
            "sender_name, is_bot, posted_at) "
            "VALUES (:id, :run, NULL, 'C1', 'collab-a', '1700000000.000200', 3, "
            "'pi_inbound', 'public', 'yes', 'PI', false, 1700000000.0)"
        ),
        {"id": message_id, "run": run_id},
    )
    return run_id, decision_id, message_id


async def test_0029_adds_two_nullable_markers_and_downgrades_back_out(scratch_db):
    """Pre-state, upgrade, post-state, downgrade, pre-state restored exactly."""
    _run_alembic(scratch_db, "0028")

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        # --- pre-state -----------------------------------------------------
        async with engine.begin() as conn:
            _run, decision_id, message_id = await _seed_run_with_one_thread_and_one_message(conn)
            before_td = await _columns(conn, "thread_decisions")
            before_am = await _columns(conn, "agent_messages")
            assert "pi_engaged_at" not in before_td
            assert "pi_inbound_state" not in before_am
            assert "reopened_at" in before_td, "0028 must already be applied"
    finally:
        await engine.dispose()

    _run_alembic(scratch_db, "head")

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        # --- post-state ----------------------------------------------------
        async with engine.connect() as conn:
            rev = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            assert rev == "0029"

            after = {
                "thread_decisions": await _columns(conn, "thread_decisions"),
                "agent_messages": await _columns(conn, "agent_messages"),
            }
            for table, column, data_type, nullable, default in _NEW_COLUMNS:
                assert column in after[table], f"0029 must add {table}.{column}"
                assert after[table][column] == (data_type, nullable, default), (
                    f"{table}.{column} must be {data_type}, nullable, with NO server "
                    f"default — got {after[table][column]}"
                )

            # No backfill: the pre-existing rows read NULL ("unknown"), which every
            # reader must treat as today's behaviour.
            engaged = (await conn.execute(
                text("SELECT pi_engaged_at FROM thread_decisions WHERE id = :id"),
                {"id": decision_id},
            )).scalar_one()
            assert engaged is None
            state = (await conn.execute(
                text("SELECT pi_inbound_state FROM agent_messages WHERE id = :id"),
                {"id": message_id},
            )).scalar_one()
            assert state is None

            # 0029 carries EXACTLY these two columns: nothing else on either table
            # moved, and no other table gained or lost a column.
            assert set(after["thread_decisions"]) - set(before_td) == {"pi_engaged_at"}
            assert set(after["agent_messages"]) - set(before_am) == {"pi_inbound_state"}
            assert set(before_td) - set(after["thread_decisions"]) == set()
            assert set(before_am) - set(after["agent_messages"]) == set()

            # Both columns are writable with the values their readers use.
            await conn.execute(
                text("UPDATE thread_decisions SET pi_engaged_at = now() WHERE id = :id"),
                {"id": decision_id},
            )
            await conn.execute(
                text("UPDATE agent_messages SET pi_inbound_state = 'handled' WHERE id = :id"),
                {"id": message_id},
            )
            await conn.commit()
    finally:
        await engine.dispose()

    _run_alembic(scratch_db, "0028", cmd="downgrade")

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        # --- pre-state restored exactly ------------------------------------
        async with engine.connect() as conn:
            rev = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            assert rev == "0028"
            assert await _columns(conn, "thread_decisions") == before_td
            assert await _columns(conn, "agent_messages") == before_am
            # The rows themselves survive the round trip.
            assert (await conn.execute(
                text("SELECT count(*) FROM thread_decisions WHERE id = :id"), {"id": decision_id},
            )).scalar_one() == 1
            assert (await conn.execute(
                text("SELECT count(*) FROM agent_messages WHERE id = :id"), {"id": message_id},
            )).scalar_one() == 1
    finally:
        await engine.dispose()


async def test_0029_downgrade_is_idempotent_when_the_columns_are_already_gone(scratch_db):
    """The 0022+ convention: downgrades are ``if_exists``-guarded, so a half-applied
    or hand-repaired database does not abort the downgrade mid stop-the-world window."""
    _run_alembic(scratch_db, "head")

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE thread_decisions DROP COLUMN pi_engaged_at"))
            await conn.execute(text("ALTER TABLE agent_messages DROP COLUMN pi_inbound_state"))
    finally:
        await engine.dispose()

    _run_alembic(scratch_db, "0028", cmd="downgrade")  # must not raise

    engine = create_async_engine(scratch_db, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rev = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            assert rev == "0028"
    finally:
        await engine.dispose()


def test_0029_is_the_only_head():
    alembic = os.path.join(os.path.dirname(sys.executable), "alembic")
    r = subprocess.run(
        [alembic, "heads"], cwd=str(_REPO_ROOT), capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    heads = [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]
    assert heads == ["0029"], r.stdout
