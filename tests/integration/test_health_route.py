import asyncio

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def test_health_ok(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def _backend_is_alive(engine, pid):
    """True while `pid` still has a row in pg_stat_activity.

    A fresh connection per poll on purpose: pg_stat_* views are snapshotted per
    transaction, so re-reading them on one connection can return the terminated
    backend forever.
    """
    async with engine.connect() as admin:
        return bool(
            await admin.scalar(
                text("SELECT count(*) FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}
            )
        )


async def test_probe_survives_its_pooled_connection_being_reaped(pg_url, engine, monkeypatch):
    """A probe whose pooled connection was terminated server-side still answers 200.

    The `client` fixture cannot show this: it repoints `src.main.get_health_engine`
    at the session's NullPool engine (tests/conftest.py:132), which opens a fresh
    connection per probe and therefore has no stale connection to hand back. So this
    test builds the app itself and lets the *real* `get_health_engine()` run — only
    the DSN is redirected, by patching the `src.config.get_settings` that the
    function imports in its own body, so the pool the probe is exercised against
    (pool_size=1, max_overflow=0) is the production one.

    Driven the way the deploy verification drove it: a real `pg_terminate_backend`
    against the real backend the probe's pooled connection is using, not a mock.
    A Postgres restart, an idle-connection reaper or an admin `pg_terminate_backend`
    all produce exactly this state, and the probe used to answer it by reporting the
    (healthy) database unavailable — one spurious unhealthy probe against a
    healthcheck that nginx's `depends_on: service_healthy` gates on.
    """
    from src import main
    from src.config import get_settings

    probe_settings = get_settings().model_copy(update={"database_url": pg_url})
    monkeypatch.setattr("src.config.get_settings", lambda: probe_settings)
    monkeypatch.setattr(main, "_health_engine", None)

    app = main.create_app()
    health_engine = None
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            # Probe 1 fills the one-connection pool.
            first = await c.get("/api/health")
            assert first.status_code == 200

            # The pool holds exactly one connection, so checking it out here is the
            # same backend probe 1 used.
            health_engine = main.get_health_engine()
            async with health_engine.connect() as conn:
                pid = await conn.scalar(text("SELECT pg_backend_pid()"))

            async with engine.connect() as admin:
                await admin.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            for _ in range(100):
                if not await _backend_is_alive(engine, pid):
                    break
                await asyncio.sleep(0.05)
            assert not await _backend_is_alive(engine, pid), (
                f"backend {pid} outlived pg_terminate_backend; the test never reached "
                "the stale-connection state it means to exercise"
            )

            second = await c.get("/api/health")
            assert second.status_code == 200, (
                "the probe reported a healthy database unavailable because its own "
                "pooled connection had been reaped server-side"
            )
            assert second.json() == {"status": "ok"}

            # ...and it answered by reconnecting, not by some accident of pooling.
            async with health_engine.connect() as conn:
                assert await conn.scalar(text("SELECT pg_backend_pid()")) != pid
    finally:
        if health_engine is not None:
            await health_engine.dispose()
